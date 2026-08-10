"""End-to-end pipeline behavior against a fake CLI.

The assertions that matter most are negative: a resumed run must NOT call
start_audio again, and --dry-run must not spawn a subprocess at all. Each
generate is a quota unit that cannot be refunded or even queried.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

from notebooklm import delivery
from notebooklm import nlm_pipeline as pipeline_mod
from notebooklm.episodes import scan_queue
from notebooklm.nlm_cli import NotebookLMError
from notebooklm.nlm_pipeline import (
    EXIT_AUTH_REQUIRED,
    EXIT_CONFIG_ERROR,
    EXIT_EPISODE_FAILED,
    EXIT_OK,
    EXIT_STOPPED_EARLY,
    OUTCOME_DELIVERED,
    OUTCOME_FAILED,
    OUTCOME_PENDING,
    OUTCOME_QUARANTINED,
    OUTCOME_SKIPPED,
    Pipeline,
)
from notebooklm.state import (
    SIDECAR_MISSING,
    SIDECAR_PRESENT,
    STATUS_DELIVERED,
    STATUS_FAILED,
    STATUS_GENERATING,
    STATUS_SOURCES_ADDED,
    STATUS_QUARANTINED,
    StateStore,
)
from notebooklm.tests.fakes import (
    FakeCLI,
    auth_error,
    generation_unavailable_error,
    make_config,
    make_episode_dir,
    rate_limit_error,
    silent_logger,
)
from notebooklm.verify import Verification

VERIFIED = Verification(True, "ok", codec="aac", container="mov,mp4,m4a",
                        duration=1100.0, size_bytes=12345)
REJECTED = Verification(False, "unexpected codec 'html'", codec="html")


@contextmanager
def verification(result: Verification = VERIFIED):
    with mock.patch.object(delivery, "verify_audio", return_value=result):
        yield


class PipelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = make_config(self.root)
        self.log = silent_logger()
        self.addCleanup(self.tmp.cleanup)

    def state(self) -> StateStore:
        return StateStore.load(self.cfg.state_file)

    def run_pipeline(self, cli: FakeCLI, *, state: StateStore | None = None,
                     limit: int | None = None,
                     result: Verification = VERIFIED) -> tuple[int, Pipeline]:
        state = state or self.state()
        pipe = Pipeline(self.cfg, cli, state, self.log, limit=limit,
                        sleep=lambda _seconds: None)
        with verification(result):
            code = pipe.run(scan_queue(self.cfg.queue_dir))
        return code, pipe

    def episode(self, name: str = "ep1", **kwargs) -> Path:
        return make_episode_dir(self.cfg.queue_dir, name, **kwargs)


class HappyPathTest(PipelineTestCase):
    def test_single_episode_reaches_the_inbox(self) -> None:
        self.episode("physics", title="Could AI Pass Physics",
                     focus="Focus on the grading rubric.",
                     sidecar="Could AI Pass Physics (2023)\n\nIn this episode we unpack...")
        cli = FakeCLI()
        code, pipe = self.run_pipeline(cli)

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(pipe.outcomes, {"Could_AI_Pass_Physics": OUTCOME_DELIVERED})
        self.assertTrue((self.cfg.inbox_dir / "Could_AI_Pass_Physics.m4a").exists())
        self.assertTrue((self.cfg.inbox_dir / "Could_AI_Pass_Physics.md").exists())

    def test_state_records_the_delivery_and_the_quota_spend(self) -> None:
        self.episode("physics", title="Deep Dive")
        code, _ = self.run_pipeline(FakeCLI())
        self.assertEqual(code, EXIT_OK)

        state = self.state()
        record = next(iter(state.episodes.values()))
        self.assertEqual(record.status, STATUS_DELIVERED)
        self.assertEqual(record.notebook_id, "nb_001")
        self.assertEqual(record.task_id, "task_001")
        self.assertEqual(record.output_path, str(self.cfg.inbox_dir / "Deep_Dive.m4a"))
        self.assertEqual(state.quota.used, 1)

    def test_focus_prompt_is_what_gets_sent(self) -> None:
        self.episode("ep", title="T", focus="Talk about V-JEPA 2-AC specifically.")
        cli = FakeCLI()
        self.run_pipeline(cli)
        start = next(c for c in cli.calls if c[0] == "start_audio")
        self.assertEqual(start[1][1], "Talk about V-JEPA 2-AC specifically.")

    def test_every_pdf_in_a_multi_paper_episode_is_uploaded(self) -> None:
        self.episode("pairing", pdfs={"a.pdf": b"%PDF a", "b.pdf": b"%PDF b"})
        cli = FakeCLI()
        self.run_pipeline(cli)
        self.assertEqual(cli.count("add_source"), 2)
        self.assertEqual(cli.count("start_audio"), 1)

    def test_missing_sidecar_still_delivers_and_is_recorded(self) -> None:
        self.episode("ep", title="No Sidecar Here")
        code, _ = self.run_pipeline(FakeCLI())
        self.assertEqual(code, EXIT_OK)
        self.assertTrue((self.cfg.inbox_dir / "No_Sidecar_Here.m4a").exists())
        self.assertFalse((self.cfg.inbox_dir / "No_Sidecar_Here.md").exists())
        record = next(iter(self.state().episodes.values()))
        self.assertEqual(record.sidecar, SIDECAR_MISSING)

    def test_sidecar_present_is_recorded(self) -> None:
        self.episode("ep", title="With Sidecar", sidecar="body text")
        self.run_pipeline(FakeCLI())
        record = next(iter(self.state().episodes.values()))
        self.assertEqual(record.sidecar, SIDECAR_PRESENT)


class ResumeTest(PipelineTestCase):
    def test_wait_timeout_leaves_the_episode_pending_not_failed(self) -> None:
        self.episode("slow", title="Slow Episode")
        cli = FakeCLI(statuses=["pending"])
        code, pipe = self.run_pipeline(cli)

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(pipe.outcomes, {"Slow_Episode": OUTCOME_PENDING})
        record = next(iter(self.state().episodes.values()))
        self.assertEqual(record.status, STATUS_GENERATING)
        self.assertEqual(record.task_id, "task_001")

    def test_second_run_polls_the_same_task_and_never_regenerates(self) -> None:
        self.episode("slow", title="Slow Episode")
        first = FakeCLI(statuses=["pending"])
        self.run_pipeline(first)
        self.assertEqual(first.count("start_audio"), 1)

        second = FakeCLI(statuses=["completed"])
        code, pipe = self.run_pipeline(second)

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(pipe.outcomes, {"Slow_Episode": OUTCOME_DELIVERED})
        self.assertEqual(second.count("start_audio"), 0, "resume must not spend another quota unit")
        self.assertEqual(second.count("create_notebook"), 0)
        self.assertGreaterEqual(second.count("poll"), 1)
        self.assertEqual(self.state().quota.used, 1, "the resumed run must not re-bill quota")

    def test_the_same_episode_is_never_generated_twice_in_one_run(self) -> None:
        """Exercises the generated_this_run guard directly.

        A rate limit rewinds the record to `sources_added` so the *next* run can
        retry it - which means a second pass inside the *same* run would
        otherwise walk straight back into a generate.
        """
        self.episode("slow", title="Slow Episode")
        cli = FakeCLI(raise_on={"start_audio": rate_limit_error()})
        pipe = Pipeline(self.cfg, cli, self.state(), self.log)
        episode = scan_queue(self.cfg.queue_dir)[0]

        with self.assertRaises(NotebookLMError):
            pipe.process(episode)
        self.assertEqual(cli.count("start_audio"), 1)
        self.assertIn(episode.key, pipe.generated_this_run)

        outcome = pipe.process(episode)
        self.assertEqual(cli.count("start_audio"), 1, "guard must block a second generate")
        self.assertEqual(outcome, OUTCOME_PENDING)

    def test_sources_are_not_re_uploaded_on_resume(self) -> None:
        self.episode("slow", title="Slow", pdfs={"a.pdf": b"%PDF a", "b.pdf": b"%PDF b"})
        self.run_pipeline(FakeCLI(statuses=["pending"]))
        second = FakeCLI(statuses=["completed"])
        self.run_pipeline(second)
        self.assertEqual(second.count("add_source"), 0)

    def test_already_delivered_episode_is_skipped_without_any_cli_call(self) -> None:
        self.episode("done", title="Done")
        self.run_pipeline(FakeCLI())
        (self.cfg.inbox_dir / "Done.m4a").unlink()  # podpub moved it out of the inbox

        cli = FakeCLI()
        code, pipe = self.run_pipeline(cli)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(pipe.outcomes, {"Done": OUTCOME_SKIPPED})
        self.assertEqual(cli.calls, [], "a delivered episode must not touch the network")

    def test_renaming_the_queue_folder_does_not_regenerate(self) -> None:
        directory = self.episode("original", title="Stable Title")
        self.run_pipeline(FakeCLI())
        (self.cfg.inbox_dir / "Stable_Title.m4a").unlink()
        directory.rename(self.cfg.queue_dir / "renamed")

        cli = FakeCLI()
        _, pipe = self.run_pipeline(cli)
        self.assertEqual(pipe.outcomes, {"Stable_Title": OUTCOME_SKIPPED})
        self.assertEqual(cli.count("start_audio"), 0)


class OrphanAdoptionTest(PipelineTestCase):
    """A crash between 'quota spent' and 'task_id saved' must not regenerate."""

    def _wedge_state(self) -> None:
        state = self.state()
        episode = scan_queue(self.cfg.queue_dir)[0]
        record = state.upsert(episode.key, slug=episode.slug, title=episode.title,
                              queue_dir=str(episode.directory))
        record.notebook_id = "nb_001"
        record.sources_added = [p.name for p in episode.pdfs]
        record.status = STATUS_GENERATING
        record.task_id = None
        state.quota.used = 1
        state.save()

    def test_existing_artifact_is_adopted_instead_of_regenerated(self) -> None:
        self.episode("orphan", title="Orphan")
        self._wedge_state()
        cli = FakeCLI(statuses=["completed"], artifacts=[{"id": "art_777", "status": "completed"}])
        code, pipe = self.run_pipeline(cli)

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(pipe.outcomes, {"Orphan": OUTCOME_DELIVERED})
        self.assertEqual(cli.count("start_audio"), 0)
        self.assertEqual(next(iter(self.state().episodes.values())).task_id, "art_777")

    def test_no_artifact_fails_that_episode_rather_than_regenerating(self) -> None:
        """Nothing to adopt means this episode needs a human - but only this
        episode, so the failure must not stop the rest of the queue."""
        self.episode("orphan", title="Orphan")
        self._wedge_state()
        cli = FakeCLI(artifacts=[])
        code, pipe = self.run_pipeline(cli)

        self.assertEqual(code, EXIT_EPISODE_FAILED)
        self.assertEqual(pipe.outcomes, {"Orphan": OUTCOME_FAILED})
        self.assertEqual(cli.count("start_audio"), 0)
        record = next(iter(self.state().episodes.values()))
        self.assertEqual(record.status, STATUS_FAILED)
        self.assertIn("Refusing to regenerate", record.last_error)
        self.assertIn("state.json", record.last_error)


class QuotaTest(PipelineTestCase):
    def test_cap_stops_the_run_and_leaves_the_rest_queued(self) -> None:
        self.cfg.daily_audio_cap = 1
        self.episode("one", title="One", pdfs={"a.pdf": b"%PDF a"})
        self.episode("two", title="Two", pdfs={"b.pdf": b"%PDF b"})

        cli = FakeCLI()
        code, pipe = self.run_pipeline(cli)

        self.assertEqual(code, EXIT_STOPPED_EARLY)
        self.assertEqual(cli.count("start_audio"), 1)
        self.assertEqual(sum(1 for o in pipe.outcomes.values() if o == OUTCOME_DELIVERED), 1)
        self.assertEqual(self.state().quota.used, 1)

    def test_a_new_day_reopens_the_budget(self) -> None:
        self.cfg.daily_audio_cap = 1
        self.episode("one", title="One", pdfs={"a.pdf": b"%PDF a"})
        self.run_pipeline(FakeCLI())

        state = self.state()
        state.quota.date = "2000-01-01"  # pretend the spend was yesterday
        state.save()

        self.episode("two", title="Two", pdfs={"b.pdf": b"%PDF b"})
        cli = FakeCLI()
        code, _ = self.run_pipeline(cli)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(cli.count("start_audio"), 1)
        self.assertEqual(self.state().quota.used, 1)

    def test_a_capped_episode_never_gets_a_notebook(self) -> None:
        """Checking the budget only at generate time left one orphan notebook
        (and an orphan PDF upload) per capped episode in the account."""
        self.cfg.daily_audio_cap = 1
        self.episode("one", title="One", pdfs={"a.pdf": b"%PDF a"})
        self.episode("two", title="Two", pdfs={"b.pdf": b"%PDF b"})

        cli = FakeCLI()
        code, _ = self.run_pipeline(cli)
        self.assertEqual(code, EXIT_STOPPED_EARLY)
        self.assertEqual(cli.count("create_notebook"), 1, "capped episode created a notebook")
        self.assertEqual(cli.count("add_source"), 1, "capped episode uploaded a PDF")

    def test_a_cap_hit_does_not_block_resuming_already_paid_work(self) -> None:
        """Episodes already generated must still be downloadable when the
        budget is gone - they cost nothing more."""
        self.episode("paid", title="Paid")
        self.run_pipeline(FakeCLI(statuses=["pending"]))  # spends the unit, leaves it pending

        self.cfg.daily_audio_cap = 1  # budget now exhausted
        cli = FakeCLI(statuses=["completed"])
        code, pipe = self.run_pipeline(cli)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(pipe.outcomes, {"Paid": OUTCOME_DELIVERED})
        self.assertEqual(cli.count("start_audio"), 0)

    def test_limit_flag_caps_episodes_processed(self) -> None:
        self.episode("one", title="One", pdfs={"a.pdf": b"%PDF a"})
        self.episode("two", title="Two", pdfs={"b.pdf": b"%PDF b"})
        cli = FakeCLI()
        self.run_pipeline(cli, limit=1)
        self.assertEqual(cli.count("start_audio"), 1)


class FailureRoutingTest(PipelineTestCase):
    def test_rate_limit_stops_the_run_without_hammering(self) -> None:
        self.episode("one", title="One", pdfs={"a.pdf": b"%PDF a"})
        self.episode("two", title="Two", pdfs={"b.pdf": b"%PDF b"})
        cli = FakeCLI(raise_on={"start_audio": rate_limit_error()})
        code, _ = self.run_pipeline(cli)

        self.assertEqual(code, EXIT_STOPPED_EARLY)
        self.assertEqual(cli.count("start_audio"), 1, "must not retry the second episode")

    def test_expired_auth_that_cannot_refresh_exits_before_doing_work(self) -> None:
        self.episode("one", title="One")
        cli = FakeCLI(auth=False, refresh_ok=False)
        code, _ = self.run_pipeline(cli)

        self.assertEqual(code, EXIT_AUTH_REQUIRED)
        self.assertEqual(cli.count("create_notebook"), 0)
        self.assertEqual(cli.count("start_audio"), 0)

    def test_refreshable_auth_continues_normally(self) -> None:
        self.episode("one", title="One")
        cli = FakeCLI(auth=False, refresh_ok=True)
        code, _ = self.run_pipeline(cli)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(cli.count("auth_refresh"), 1)

    def test_mid_run_auth_error_stops_the_run(self) -> None:
        self.episode("one", title="One")
        cli = FakeCLI(raise_on={"create_notebook": auth_error()})
        code, _ = self.run_pipeline(cli)
        self.assertEqual(code, EXIT_AUTH_REQUIRED)

    def test_server_side_generation_failure_is_reported_as_a_failure(self) -> None:
        """A terminal server-side failure is not 'pending'. Reporting it as
        pending (exit 0) tells the agent to re-run and wait, but the record is
        terminal - nothing would ever poll it."""
        self.episode("one", title="One")
        cli = FakeCLI(statuses=["failed"])
        code, pipe = self.run_pipeline(cli)

        self.assertEqual(code, EXIT_EPISODE_FAILED)
        self.assertEqual(pipe.outcomes, {"One": OUTCOME_FAILED})
        record = next(iter(self.state().episodes.values()))
        self.assertEqual(record.status, STATUS_FAILED)
        self.assertEqual(record.last_error, "server-side failure")

    def test_a_failed_episode_is_skipped_on_the_next_run(self) -> None:
        self.episode("one", title="One")
        self.run_pipeline(FakeCLI(statuses=["failed"]))
        cli = FakeCLI(statuses=["completed"])
        _, pipe = self.run_pipeline(cli)
        self.assertEqual(pipe.outcomes, {"One": OUTCOME_SKIPPED})
        self.assertEqual(cli.count("start_audio"), 0)

    def test_unverifiable_download_is_quarantined_not_delivered(self) -> None:
        self.episode("bad", title="Bad Audio")
        cli = FakeCLI()
        code, pipe = self.run_pipeline(cli, result=REJECTED)

        self.assertEqual(code, EXIT_EPISODE_FAILED)
        self.assertEqual(pipe.outcomes, {"Bad_Audio": OUTCOME_QUARANTINED})
        self.assertFalse((self.cfg.inbox_dir / "Bad_Audio.m4a").exists())
        self.assertEqual(len(list(self.cfg.quarantine_dir.glob("*.m4a"))), 1)
        record = next(iter(self.state().episodes.values()))
        self.assertEqual(record.status, STATUS_QUARANTINED)


class SourceReadinessTest(PipelineTestCase):
    """NotebookLM rejects `generate audio` while sources are still ingesting.

    Seen on the first real run: 3 PDFs (34MB), generate fired immediately, the
    server answered "Audio generation is unavailable"; ~90s later every source
    read `ready` and the retry worked.
    """

    def test_generation_waits_until_every_source_is_ready(self) -> None:
        self.episode("ep", title="Ingesting", pdfs={"a.pdf": b"%PDF a", "b.pdf": b"%PDF b"})
        cli = FakeCLI(source_statuses=[
            ["preparing", "processing"],
            ["ready", "processing"],
            ["ready", "ready"],
        ])
        code, pipe = self.run_pipeline(cli)

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(pipe.outcomes, {"Ingesting": OUTCOME_DELIVERED})
        self.assertEqual(cli.count("list_sources"), 3)
        # The gate must come before the generate, not after it.
        self.assertLess(cli.names().index("list_sources"), cli.names().index("start_audio"))

    def test_readiness_gate_runs_before_any_quota_is_spent(self) -> None:
        self.episode("ep", title="Never Ready")
        self.cfg.generate.source_ready_timeout = 0  # one check, then give up
        cli = FakeCLI(source_statuses=[["processing"]])
        code, pipe = self.run_pipeline(cli)

        self.assertEqual(code, EXIT_EPISODE_FAILED)
        self.assertEqual(pipe.outcomes, {"Never_Ready": OUTCOME_FAILED})
        self.assertEqual(cli.count("start_audio"), 0)
        self.assertEqual(self.state().quota.used, 0, "a readiness timeout must cost no quota")
        record = next(iter(self.state().episodes.values()))
        self.assertEqual(record.status, STATUS_FAILED)
        self.assertIn("still ingesting", record.last_error)

    def test_a_source_that_fails_to_ingest_fails_only_that_episode(self) -> None:
        self.episode("bad", title="Bad Source", pdfs={"a.pdf": b"%PDF a"})
        self.episode("good", title="Good One", pdfs={"b.pdf": b"%PDF b"})
        # First episode's source errors; the second one is fine.
        cli = FakeCLI(source_statuses=[["error"], ["ready"]])
        code, pipe = self.run_pipeline(cli)

        self.assertEqual(code, EXIT_EPISODE_FAILED)
        self.assertEqual(pipe.outcomes.get("Bad_Source"), OUTCOME_FAILED)
        self.assertEqual(pipe.outcomes.get("Good_One"), OUTCOME_DELIVERED)
        self.assertEqual(cli.count("start_audio"), 1)

    def test_a_resumed_episode_does_not_wait_on_sources_again(self) -> None:
        self.episode("slow", title="Slow")
        self.run_pipeline(FakeCLI(statuses=["pending"]))

        cli = FakeCLI(statuses=["completed"])
        self.run_pipeline(cli)
        self.assertEqual(cli.count("list_sources"), 0,
                         "resuming an already-generating episode needs no ingestion wait")


class GenerationUnavailableTest(PipelineTestCase):
    def test_refusal_refunds_quota_and_leaves_the_episode_retryable(self) -> None:
        """The exact error from the first real run. Nothing was generated, so
        the unit is refunded and the record rewinds to `sources_added`."""
        self.episode("ep", title="Refused")
        cli = FakeCLI(raise_on={"start_audio": generation_unavailable_error()})
        code, pipe = self.run_pipeline(cli)

        self.assertEqual(code, EXIT_EPISODE_FAILED)
        self.assertEqual(pipe.outcomes, {"Refused": OUTCOME_FAILED})
        self.assertEqual(self.state().quota.used, 0, "nothing was generated; refund the unit")
        record = next(iter(self.state().episodes.values()))
        self.assertEqual(record.status, STATUS_SOURCES_ADDED)
        self.assertIsNone(record.task_id)

    def test_refusal_does_not_stop_the_rest_of_the_queue(self) -> None:
        """Unlike a rate limit, this says nothing about the other episodes -
        they have their own notebooks and their own ingestion state."""
        self.episode("one", title="Refused One", pdfs={"a.pdf": b"%PDF a"})
        self.episode("two", title="Fine Two", pdfs={"b.pdf": b"%PDF b"})
        cli = FakeCLI(raise_on={"start_audio": generation_unavailable_error()})
        # Only the first start_audio should raise; clear it once consumed.
        original = cli.start_audio

        def start_once(notebook_id, prompt, **kwargs):
            try:
                return original(notebook_id, prompt, **kwargs)
            finally:
                cli.raise_on.pop("start_audio", None)

        cli.start_audio = start_once  # type: ignore[method-assign]

        code, pipe = self.run_pipeline(cli)
        self.assertEqual(code, EXIT_EPISODE_FAILED)
        self.assertEqual(pipe.outcomes.get("Refused_One"), OUTCOME_FAILED)
        self.assertEqual(pipe.outcomes.get("Fine_Two"), OUTCOME_DELIVERED)

    def test_the_next_run_retries_a_refused_episode(self) -> None:
        self.episode("ep", title="Refused")
        self.run_pipeline(FakeCLI(raise_on={"start_audio": generation_unavailable_error()}))

        cli = FakeCLI()
        code, pipe = self.run_pipeline(cli)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(pipe.outcomes, {"Refused": OUTCOME_DELIVERED})
        self.assertEqual(cli.count("create_notebook"), 0, "the notebook is reused")
        self.assertEqual(cli.count("start_audio"), 1)
        self.assertEqual(self.state().quota.used, 1)


class VersionPinTest(PipelineTestCase):
    def test_an_unverified_cli_version_refuses_to_run(self) -> None:
        self.episode("one", title="One")
        cli = FakeCLI()
        cli.version = lambda: "NotebookLM CLI, version 0.9.3 (deadbeef)"  # type: ignore[method-assign]
        code, _ = self.run_pipeline(cli)

        self.assertEqual(code, EXIT_CONFIG_ERROR)
        self.assertEqual(cli.count("create_notebook"), 0)
        self.assertEqual(cli.count("start_audio"), 0)

    def test_the_pinned_version_runs_normally(self) -> None:
        self.episode("one", title="One")
        code, _ = self.run_pipeline(FakeCLI())
        self.assertEqual(code, EXIT_OK)


class MinimumDurationTest(PipelineTestCase):
    def test_a_two_second_file_is_quarantined_not_published(self) -> None:
        self.episode("short", title="Short")
        self.cfg.min_duration_sec = 300
        # Real verifier, stubbed ffprobe: a perfectly valid 2-second AAC file.
        probe_data = {
            "streams": [{"codec_type": "audio", "codec_name": "aac"}],
            "format": {"format_name": "mov,mp4,m4a", "duration": "2.0"},
        }
        cli = FakeCLI()
        with mock.patch("notebooklm.verify.probe", return_value=probe_data):
            pipe = Pipeline(self.cfg, cli, self.state(), self.log)
            code = pipe.run(scan_queue(self.cfg.queue_dir))

        self.assertEqual(code, EXIT_EPISODE_FAILED)
        self.assertEqual(pipe.outcomes, {"Short": OUTCOME_QUARANTINED})
        self.assertFalse((self.cfg.inbox_dir / "Short.m4a").exists())
        self.assertEqual(len(list(self.cfg.quarantine_dir.glob("*.m4a"))), 1)
        record = next(iter(self.state().episodes.values()))
        self.assertIn("below the 300s minimum", record.last_error)


class SidecarCollisionTest(PipelineTestCase):
    def test_an_existing_inbox_sidecar_is_never_overwritten(self) -> None:
        """INSTRUCTIONS tells agents to write the .md straight into inbox/ when
        one is missing. That hand-written file must win."""
        self.episode("ep", title="Keep Mine", sidecar="queued version")
        self.cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.inbox_dir / "Keep_Mine.md").write_text("hand-written version", encoding="utf-8")

        code, pipe = self.run_pipeline(FakeCLI())
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(pipe.outcomes, {"Keep_Mine": OUTCOME_DELIVERED})
        self.assertEqual((self.cfg.inbox_dir / "Keep_Mine.md").read_text(encoding="utf-8"),
                         "hand-written version")


class DryRunTest(PipelineTestCase):
    def test_dry_run_makes_no_subprocess_calls_and_writes_no_state(self) -> None:
        self.episode("one", title="One", sidecar="body")
        with mock.patch.object(pipeline_mod, "load_config", return_value=self.cfg), \
             mock.patch.object(pipeline_mod, "setup_logging", return_value=self.log), \
             mock.patch("subprocess.run",
                        side_effect=AssertionError("dry run must not spawn a process")):
            code = pipeline_mod.main(["run", "--dry-run"])

        self.assertEqual(code, EXIT_OK)
        self.assertFalse(self.cfg.state_file.exists(), "dry run must not write state.json")
        self.assertFalse(self.cfg.lock_file.exists(), "dry run must not take the lock")
        self.assertEqual(list(self.cfg.inbox_dir.iterdir()), [])

    def test_dry_run_writes_no_log_file_and_creates_no_directories(self) -> None:
        """'Makes no network calls and writes no state' has to mean the disk too."""
        cfg = make_config(self.root / "pristine")
        for path in (cfg.tmp_dir, cfg.quarantine_dir, cfg.log_file.parent, cfg.queue_dir):
            if path.exists():
                shutil.rmtree(path)

        # Real setup_logging here - it is what decides whether a file is opened.
        with mock.patch.object(pipeline_mod, "load_config", return_value=cfg), \
             mock.patch("subprocess.run", side_effect=AssertionError("no calls")), \
             redirect_stdout(io.StringIO()):
            code = pipeline_mod.main(["run", "--dry-run"])

        self.assertEqual(code, EXIT_OK)
        self.assertFalse(cfg.log_file.exists(), "dry run created a log file")
        self.assertFalse(cfg.tmp_dir.exists(), "dry run created tmp/")
        self.assertFalse(cfg.quarantine_dir.exists(), "dry run created quarantine/")

    def test_dry_run_on_an_empty_queue_is_fine(self) -> None:
        with mock.patch.object(pipeline_mod, "load_config", return_value=self.cfg), \
             mock.patch.object(pipeline_mod, "setup_logging", return_value=self.log), \
             mock.patch("subprocess.run", side_effect=AssertionError("no calls")):
            self.assertEqual(pipeline_mod.main(["run", "--dry-run"]), EXIT_OK)

    def test_dry_run_reports_poll_not_regenerate_for_in_flight_work(self) -> None:
        self.episode("slow", title="Slow")
        self.run_pipeline(FakeCLI(statuses=["pending"]))

        messages: list[str] = []
        logger = mock.Mock()
        logger.info.side_effect = lambda fmt, *a: messages.append(fmt % a if a else fmt)
        state = self.state()
        pipeline_mod.print_plan(self.cfg, state, scan_queue(self.cfg.queue_dir), logger)
        self.assertTrue(any("poll existing task" in m for m in messages), messages)


class StatusTest(PipelineTestCase):
    def test_status_reports_quota_lock_and_queue(self) -> None:
        self.episode("one", title="One")
        messages: list[str] = []
        logger = mock.Mock()
        logger.info.side_effect = lambda fmt, *a: messages.append(fmt % a if a else fmt)
        pipeline_mod.print_status(self.cfg, self.state(), FakeCLI(), logger)

        joined = "\n".join(messages)
        self.assertIn("quota:", joined)
        self.assertIn("lock:    free", joined)
        self.assertIn("One", joined)
        self.assertIn("SIDECAR MISSING", joined)


if __name__ == "__main__":
    unittest.main()
