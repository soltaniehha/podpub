"""Regression tests for behaviors that once did not match the documented design.

Each class was written from black-box testing against README.md and
automation/INSTRUCTIONS.md, and each started life as an `expectedFailure`
describing a real defect. The defects are fixed and the decorators are gone, so
these now guard against the behavior coming back. The docstrings keep the
original diagnosis - that is the record of *why* each rule exists.
"""

from __future__ import annotations

import unittest
from unittest import mock

from notebooklm import nlm_pipeline as pipeline_mod
from notebooklm.episodes import scan_queue
from notebooklm.nlm_pipeline import (
    EXIT_CONFIG_ERROR,
    OUTCOME_DELIVERED,
    Pipeline,
)
from notebooklm.tests.fakes import FakeCLI, rate_limit_error, silent_logger
from notebooklm.tests.test_pipeline import PipelineTestCase
from notebooklm.state import STATUS_GENERATING


class InboxCollisionTest(PipelineTestCase):
    """delivery.py:86 tells the user the download is 'waiting at <tmp>' and to
    re-run after clearing the inbox. nlm_pipeline.py:399-406 then quarantines
    that file and marks the record `quarantined`, so the re-run skips the
    episode instead of delivering it."""

    def _collide(self) -> None:
        self.episode("ep", title="Collide")
        self.cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.inbox_dir / "Collide.m4a").write_bytes(b"an-earlier-episode")
        self.run_pipeline(FakeCLI())

    def test_download_is_left_where_the_error_message_says_it_is(self) -> None:
        self._collide()
        record = next(iter(self.state().episodes.values()))
        promised = record.last_error.rsplit("waiting at ", 1)[1].rstrip(".")
        self.assertTrue(
            self.cfg.tmp_dir.joinpath("Collide.m4a").exists(),
            f"error promises the file is at {promised}, but it was moved to quarantine",
        )

    def test_rerun_after_clearing_the_inbox_delivers_the_episode(self) -> None:
        self._collide()
        (self.cfg.inbox_dir / "Collide.m4a").unlink()  # user published the old one
        _, pipe = self.run_pipeline(FakeCLI())
        self.assertEqual(pipe.outcomes, {"Collide": OUTCOME_DELIVERED})


class WedgedEpisodeBlocksTheQueueTest(PipelineTestCase):
    """An episode stuck in `generating` with no task_id and no adoptable
    artifact raises StopRun, which breaks the whole loop (nlm_pipeline.py:277).
    Every other queued episode is skipped, on this run and on every run after,
    until a human edits state.json."""

    def _wedge_first_episode(self) -> None:
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

    def test_a_healthy_episode_still_runs_when_a_sibling_is_wedged(self) -> None:
        self.episode("wedged", title="Wedged One", pdfs={"a.pdf": b"%PDF a"})
        self._wedge_first_episode()
        self.episode("healthy", title="Healthy Two", pdfs={"b.pdf": b"%PDF b"})

        cli = FakeCLI(artifacts=[])
        _, pipe = self.run_pipeline(cli)
        self.assertEqual(pipe.outcomes.get("Healthy_Two"), OUTCOME_DELIVERED)
        self.assertTrue((self.cfg.inbox_dir / "Healthy_Two.m4a").exists())


class RateLimitRecoveryTest(PipelineTestCase):
    """A rate limit raised by `generate audio` leaves the record in
    `generating` with no task_id (quota was debited first, by design). Nothing
    was actually generated, so the next run's orphan adoption finds no artifact
    and hard-stops with exit 5 - a transient server condition turns into a
    permanent wedge needing manual state surgery."""

    def test_the_next_run_recovers_once_the_rate_limit_clears(self) -> None:
        self.episode("ep", title="Rate Limited")
        self.run_pipeline(FakeCLI(raise_on={"start_audio": rate_limit_error()}))

        code, pipe = self.run_pipeline(FakeCLI(artifacts=[]))
        self.assertNotEqual(code, EXIT_CONFIG_ERROR,
                            "a transient rate limit must not require editing state.json")
        self.assertEqual(pipe.outcomes.get("Rate_Limited"), OUTCOME_DELIVERED)


class DuplicateContentTest(PipelineTestCase):
    """episode_key is the hash of the PDF contents, so two queue folders holding
    the same paper share one record. The second folder skips generation (good)
    but still downloads and delivers the first episode's audio under its own
    slug - one generation becomes two identical podcast episodes."""

    def test_the_same_paper_in_two_folders_delivers_one_episode(self) -> None:
        same = {"paper.pdf": b"%PDF identical"}
        self.episode("alpha", title="Alpha", pdfs=same)
        self.episode("beta", title="Beta", pdfs=same)

        cli = FakeCLI()
        self.run_pipeline(cli)
        self.assertEqual(cli.count("start_audio"), 1)
        delivered = sorted(p.name for p in self.cfg.inbox_dir.glob("*.m4a"))
        self.assertEqual(len(delivered), 1, f"one generation delivered twice: {delivered}")


class DryRunAccuracyTest(PipelineTestCase):
    """print_plan has no branch for `generating` + no task_id, so it falls
    through to the 'create notebook … generate (1 quota unit)' line. The real
    run would either adopt an existing artifact or stop with exit 5; it would
    never generate. INSTRUCTIONS.md Step 3 tells the agent to trust this line."""

    def test_plan_does_not_promise_a_generate_for_a_wedged_episode(self) -> None:
        self.episode("ep", title="Wedged")
        self.run_pipeline(FakeCLI(statuses=["pending"]))
        state = self.state()
        record = next(iter(state.episodes.values()))
        record.task_id = None
        state.save()

        messages: list[str] = []
        logger = mock.Mock()
        logger.info.side_effect = lambda fmt, *a: messages.append(fmt % a if a else fmt)
        pipeline_mod.print_plan(self.cfg, self.state(),
                                scan_queue(self.cfg.queue_dir), logger)
        actions = [m for m in messages if "action:" in m]
        self.assertFalse(any("generate" in m for m in actions), actions)


class MissingBinaryExitCodeTest(PipelineTestCase):
    """A missing `notebooklm` binary surfaces through auth_ok()/auth_refresh()
    as a failed auth probe, so the run exits 2 ('MANUAL RE-LOGIN NEEDED')
    instead of 5. INSTRUCTIONS.md maps exit 2 to 'tell the user to run
    notebooklm login', which cannot help when the tool is not installed."""

    def test_uninstalled_cli_exits_with_the_config_error_code(self) -> None:
        from notebooklm.nlm_cli import NotebookLMCLI

        self.episode("ep", title="Any")
        cli = NotebookLMCLI(binary="definitely-not-installed-notebooklm-xyz",
                            log=silent_logger("nlm.test.cli"))
        pipe = Pipeline(self.cfg, cli, self.state(), self.log)
        code = pipe.run(scan_queue(self.cfg.queue_dir))
        self.assertEqual(code, EXIT_CONFIG_ERROR)


if __name__ == "__main__":
    unittest.main()
