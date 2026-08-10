#!/usr/bin/env python3
"""nlm_pipeline - generate NotebookLM Deep Dives and hand them to podpub.

Usage:
  .venv/bin/python notebooklm/nlm_pipeline.py run [--dry-run]
  .venv/bin/python notebooklm/nlm_pipeline.py status

Reads notebooklm/queue/<episode>/, drives the external `notebooklm` CLI to
create a notebook, upload the PDFs, generate a long Deep Dive, then verifies
the download and moves it into podpub's inbox/ for `podpub.py` to publish.

Every step is resumable: progress lives in notebooklm/state.json, and a run
that is interrupted mid-generation polls the existing task on the next run
rather than spending another quota unit. See README.md for the state machine
and automation/INSTRUCTIONS.md for the agent-facing playbook.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

if __package__ in (None, ""):  # allow `python notebooklm/nlm_pipeline.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notebooklm import episodes as episodes_mod
from notebooklm.config import CONFIG_FILE, Config, load_config
from notebooklm.delivery import (
    DeliveryError,
    InboxOccupiedError,
    deliver,
    quarantine,
    temp_target,
)
from notebooklm.episodes import QueuedEpisode
from notebooklm.locking import LockBusyError, PipelineLock, read_lock
from notebooklm.nlm_cli import (
    PINNED_INSTALL_SPEC,
    SOURCE_ERROR,
    SOURCE_READY,
    VERIFIED_CLI_VERSION,
    GenerationStatus,
    NotebookLMCLI,
    NotebookLMError,
    NotebookLMNotInstalled,
    version_matches,
)
from notebooklm.scrub import scrub_text
from notebooklm.state import (
    SIDECAR_MISSING,
    SIDECAR_PRESENT,
    STATUS_DELIVERED,
    STATUS_FAILED,
    STATUS_GENERATED,
    STATUS_GENERATING,
    STATUS_NOTEBOOK_CREATED,
    STATUS_QUARANTINED,
    STATUS_SOURCES_ADDED,
    EpisodeRecord,
    StateCorruptError,
    StateStore,
)
from notebooklm.verify import FfprobeMissingError

# ---------- exit codes ----------

EXIT_OK = 0
EXIT_EPISODE_FAILED = 1
EXIT_AUTH_REQUIRED = 2
EXIT_STOPPED_EARLY = 3      # daily cap reached or rate limited
EXIT_LOCK_BUSY = 4
EXIT_CONFIG_ERROR = 5

# ---------- per-episode outcomes ----------

OUTCOME_DELIVERED = "delivered"
OUTCOME_SKIPPED = "skipped"
OUTCOME_PENDING = "pending"
OUTCOME_FAILED = "failed"
OUTCOME_QUARANTINED = "quarantined"


class StopRun(Exception):
    """Abort the remaining episodes cleanly (auth, rate limit, quota)."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ---------- logging ----------

LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 5


def setup_logging(log_file: Path, verbose: bool = False, *, to_file: bool = True) -> logging.Logger:
    """Configure the `nlm` logger tree.

    `to_file=False` keeps a read-only command (--dry-run, status) from creating
    directories or writing anything.
    """
    root = logging.getLogger("nlm")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    if to_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
        )
        root.addHandler(file_handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(stream)
    root.propagate = False
    return root


# ---------- pipeline ----------

class Pipeline:
    """Drives queued episodes through the state machine.

    `cli` is any object with NotebookLMCLI's method surface; tests pass a fake.
    """

    def __init__(self, cfg: Config, cli: NotebookLMCLI, state: StateStore,
                 log: logging.Logger, *, limit: int | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.cli = cli
        self.state = state
        self.log = log
        self.limit = limit
        self.sleep = sleep  # injectable so tests never actually wait
        self.generated_this_run: set[str] = set()
        self.outcomes: dict[str, str] = {}

    # ----- preflight -----

    def check_auth(self) -> None:
        ok, detail = self.cli.auth_ok()
        if ok:
            self.log.info("auth: ok")
            return
        self.log.warning("auth check failed (%s); attempting one keepalive refresh", detail)
        refreshed, refresh_detail = self.cli.auth_refresh()
        if refreshed:
            ok, detail = self.cli.auth_ok()
            if ok:
                self.log.info("auth: refreshed and healthy")
                return
        raise StopRun(
            "MANUAL RE-LOGIN NEEDED: cookies are stale and `notebooklm auth refresh` did not "
            f"recover them ({refresh_detail if not refreshed else detail}). "
            "Run `notebooklm login` interactively, then re-run this pipeline.",
            EXIT_AUTH_REQUIRED,
        )

    def check_cli_version(self) -> None:
        """Refuse to drive a CLI version this wrapper has not been verified against.

        The failure mode we are protecting against is silent: a renamed JSON
        field reads as "no task_id", which looks like "generation never
        started" and invites a regenerate - one wasted quota unit per episode.
        """
        version = self.cli.version()
        if version_matches(version):
            self.log.info("cli: %s", version)
            return
        raise StopRun(
            f"notebooklm CLI reports {version!r}, but this pipeline is verified against "
            f"{VERIFIED_CLI_VERSION}. Refusing to run: an unverified response shape can cost "
            f"a quota unit. Either reinstall the pinned version "
            f"(uv tool install --python 3.13 {PINNED_INSTALL_SPEC}) or re-verify the CLI "
            f"CONTRACT section of notebooklm/nlm_cli.py and update VERIFIED_CLI_VERSION.",
            EXIT_CONFIG_ERROR,
        )

    # ----- main loop -----

    def run(self, queued: list[QueuedEpisode]) -> int:
        queued = self._drop_duplicate_keys(queued)
        pending = [e for e in queued if not self._already_delivered(e)]
        self.log.info("queue: %d episode(s), %d not yet delivered", len(queued), len(pending))
        if self.limit is not None:
            pending = pending[: self.limit]

        if not pending:
            self.log.info("nothing to do")
            return EXIT_OK

        try:
            self.check_cli_version()
            self.check_auth()
        except StopRun as exc:
            self.log.error("%s", exc)
            return exc.exit_code
        except NotebookLMNotInstalled as exc:
            self.log.error("%s", exc)
            return EXIT_CONFIG_ERROR

        stop: StopRun | None = None
        for episode in pending:
            try:
                self.outcomes[episode.slug] = self.process(episode)
            except StopRun as exc:
                stop = exc
                self.log.warning("stopping run: %s", exc)
                break
            except NotebookLMError as exc:
                if exc.is_rate_limit:
                    stop = StopRun(
                        f"rate limited by NotebookLM ({exc.message}). Backing off - the next "
                        f"scheduled run picks up where this stopped. Do not retry immediately.",
                        EXIT_STOPPED_EARLY,
                    )
                    self.log.warning("stopping run: %s", stop)
                    break
                if exc.is_auth:
                    stop = StopRun(
                        f"MANUAL RE-LOGIN NEEDED: {exc.message}. Run `notebooklm login`.",
                        EXIT_AUTH_REQUIRED,
                    )
                    self.log.error("stopping run: %s", stop)
                    break
                self._fail(episode, str(exc))
            except FfprobeMissingError as exc:
                stop = StopRun(str(exc), EXIT_CONFIG_ERROR)
                self.log.error("stopping run: %s", exc)
                break
            except OSError as exc:
                self._fail(episode, f"filesystem error: {exc}")
            finally:
                self.state.save()

        self.state.save()
        self._summarize()

        if stop is not None:
            return stop.exit_code
        if any(o in (OUTCOME_FAILED, OUTCOME_QUARANTINED) for o in self.outcomes.values()):
            return EXIT_EPISODE_FAILED
        return EXIT_OK

    def _drop_duplicate_keys(self, queued: list[QueuedEpisode]) -> list[QueuedEpisode]:
        """Two folders holding the same PDFs are the same episode.

        They share one state record, so without this the second folder would
        skip generation but still download and deliver the first one's audio -
        one generation published as two episodes.
        """
        seen: dict[str, QueuedEpisode] = {}
        unique: list[QueuedEpisode] = []
        for episode in queued:
            first = seen.get(episode.key)
            if first is not None:
                self.log.warning(
                    "skipping %s: identical PDF content to %s (episode key %s). Delete one "
                    "folder, or change the papers, if these were meant to be two episodes.",
                    episode.directory.name, first.directory.name, episode.key[:12],
                )
                self.outcomes[episode.slug] = OUTCOME_SKIPPED
                continue
            seen[episode.key] = episode
            unique.append(episode)
        return unique

    def _already_delivered(self, episode: QueuedEpisode) -> bool:
        record = self.state.get(episode.key)
        if record is not None and record.is_delivered:
            self.log.info("skip %s: already delivered to %s", episode.slug, record.output_path)
            self.outcomes[episode.slug] = OUTCOME_SKIPPED
            return True
        return False

    # ----- per-episode state machine -----

    def process(self, episode: QueuedEpisode) -> str:
        record = self.state.upsert(
            episode.key,
            slug=episode.slug,
            title=episode.title,
            queue_dir=str(episode.directory),
        )
        record.sidecar = SIDECAR_PRESENT if episode.has_sidecar else SIDECAR_MISSING
        self.log.info("=== %s (%s) [%s]", episode.title, episode.key[:12], record.status)

        # Re-check here as well as up front: a duplicate folder resolves to this
        # same record, and delivery may have happened earlier in this very run.
        if record.is_delivered:
            self.log.info("skip %s: already delivered to %s", episode.slug, record.output_path)
            return OUTCOME_SKIPPED

        if record.status in (STATUS_QUARANTINED, STATUS_FAILED):
            self.log.warning(
                "skip %s: last run ended in %s (%s). Clear the record in state.json after "
                "checking NotebookLM for an existing artifact.",
                episode.slug, record.status, record.last_error,
            )
            return OUTCOME_SKIPPED

        # Check the budget before creating anything: a cap hit after notebook
        # creation leaves an orphan notebook per episode in the account.
        self._require_quota_headroom(record, episode)

        self._ensure_notebook(record, episode)
        self._ensure_sources(record, episode)

        # Only gate when a generation is actually about to be issued. Resuming
        # (task_id present) or adopting needs no ingestion wait.
        if record.status in (STATUS_NOTEBOOK_CREATED, STATUS_SOURCES_ADDED) and not record.task_id:
            if not self._await_sources_ready(record):
                return OUTCOME_FAILED

        status = self._ensure_generation(record, episode)
        if status is None:
            return OUTCOME_FAILED if record.status == STATUS_FAILED else OUTCOME_PENDING
        if status.is_failed:
            # Terminal server-side failure. The docs must not tell the agent to
            # "re-run and it will poll" - this needs a human decision.
            return OUTCOME_FAILED
        if not status.is_complete:
            return OUTCOME_PENDING
        return self._download_and_deliver(record, episode)

    def _require_quota_headroom(self, record: EpisodeRecord, episode: QueuedEpisode) -> None:
        """Stop before spending effort on an episode we cannot finish today."""
        if record.task_id or record.status in (STATUS_GENERATING, STATUS_GENERATED):
            return  # already paid for; resuming costs nothing
        cap = self.cfg.daily_audio_cap
        if self.state.quota.remaining(cap) > 0:
            return
        raise StopRun(
            f"daily audio cap reached ({self.state.quota.used}/{cap} on "
            f"{self.state.quota.date}); {episode.slug} and anything after it stay queued for "
            f"the next run.",
            EXIT_STOPPED_EARLY,
        )

    def _ensure_notebook(self, record: EpisodeRecord, episode: QueuedEpisode) -> None:
        if record.notebook_id:
            self.log.info("notebook: reusing %s", record.notebook_id)
            return
        record.notebook_id = self.cli.create_notebook(episode.title)
        record.status = STATUS_NOTEBOOK_CREATED
        record.touch()
        self.state.save()
        self.log.info("notebook: created %s", record.notebook_id)

    def _ensure_sources(self, record: EpisodeRecord, episode: QueuedEpisode) -> None:
        assert record.notebook_id
        for pdf in episode.pdfs:
            if pdf.name in record.sources_added:
                self.log.info("source: %s already added", pdf.name)
                continue
            self.cli.add_source(record.notebook_id, pdf)
            record.sources_added.append(pdf.name)
            record.touch()
            self.state.save()
            self.log.info("source: added %s", pdf.name)
        if record.status == STATUS_NOTEBOOK_CREATED:
            record.status = STATUS_SOURCES_ADDED
            record.touch()
            self.state.save()

    def _await_sources_ready(self, record: EpisodeRecord) -> bool:
        """Block until every uploaded source reports `ready`.

        NotebookLM rejects `generate audio` outright while sources are still
        ingesting ("Audio generation is unavailable"), so this runs *before* the
        quota debit - a wait that gives up here has cost nothing. Returns False
        after marking the episode failed, which fails only this episode.
        """
        assert record.notebook_id
        timeout = self.cfg.generate.source_ready_timeout
        interval = max(1, self.cfg.generate.interval)
        deadline = time.monotonic() + timeout

        while True:
            sources = self.cli.list_sources(record.notebook_id)
            pending = {
                str(s.get("title") or s.get("id") or "?"): str(s.get("status") or "unknown")
                for s in sources
                if str(s.get("status") or "unknown") != SOURCE_READY
            }
            failed = {name: st for name, st in pending.items() if st == SOURCE_ERROR}

            if failed:
                self._mark_failed(
                    record,
                    f"NotebookLM could not ingest {len(failed)} source(s): "
                    f"{', '.join(sorted(failed))}. Re-check the PDF(s); a re-upload needs a "
                    f"fresh notebook, so clear this episode's record in {self.state.path} "
                    f"once the file is fixed.",
                )
                return False

            if sources and not pending:
                self.log.info("sources: all %d ready", len(sources))
                return True

            if time.monotonic() >= deadline:
                detail = ", ".join(f"{n}={s}" for n, s in sorted(pending.items())) or "none listed"
                self._mark_failed(
                    record,
                    f"sources were still ingesting after {timeout}s ({detail}). No quota was "
                    f"spent. Large PDFs can take minutes; re-run later, or raise "
                    f"generate.source_ready_timeout in config.yaml.",
                )
                return False

            self.log.info("sources: waiting for %d of %d to finish ingesting (%s)",
                          len(pending), len(sources) or len(pending),
                          ", ".join(f"{n}={s}" for n, s in sorted(pending.items())) or "none listed")
            self.sleep(interval)

    def _ensure_generation(self, record: EpisodeRecord, episode: QueuedEpisode) -> GenerationStatus | None:
        """Return a terminal-or-pending status, generating at most once per run."""
        assert record.notebook_id

        if record.status == STATUS_GENERATED and record.task_id:
            return GenerationStatus(task_id=record.task_id, status="completed",
                                    url=record.artifact.get("url"))

        if record.status == STATUS_GENERATING and not record.task_id:
            # A run died between "quota spent" and "task_id saved". Adopt the
            # artifact that generation almost certainly created instead of
            # paying for another one.
            adopted = self._adopt_orphan(record)
            if adopted is None:
                # This episode needs a human, but it is only one episode: failing
                # it here (rather than stopping the run) keeps its siblings moving.
                self._mark_failed(
                    record,
                    f"generation was recorded as started but no task_id was saved, and notebook "
                    f"{record.notebook_id} contains no audio artifact. Refusing to regenerate "
                    f"blindly - that would spend another quota unit. Open the notebook in the "
                    f"web UI: if it is genuinely empty, clear this episode's `status`, "
                    f"`task_id` and `attempts` in {self.state.path} to allow one retry.",
                )
                return None
            record.task_id = adopted
            record.touch()
            self.state.save()

        if record.task_id:
            return self._await_task(record)

        return self._start_generation(record, episode)

    def _mark_failed(self, record: EpisodeRecord, message: str) -> None:
        record.status = STATUS_FAILED
        record.last_error = scrub_text(message)
        record.touch()
        self.state.save()
        self.log.error("%s: %s", record.slug, record.last_error)

    def _adopt_orphan(self, record: EpisodeRecord) -> str | None:
        assert record.notebook_id
        artifacts = self.cli.list_audio_artifacts(record.notebook_id)
        if not artifacts:
            return None
        artifact_id = artifacts[0].get("id")
        if not artifact_id:
            return None
        self.log.warning(
            "adopting existing audio artifact %s (title=%r, status=%s) - no task_id was in "
            "state; %d audio artifact(s) in notebook %s",
            artifact_id, artifacts[0].get("title"), artifacts[0].get("status"),
            len(artifacts), record.notebook_id,
        )
        return str(artifact_id)

    def _start_generation(self, record: EpisodeRecord, episode: QueuedEpisode) -> GenerationStatus | None:
        if episode.key in self.generated_this_run:
            self.log.warning("%s: already generated once this run; not generating again",
                             episode.slug)
            return None

        cap = self.cfg.daily_audio_cap
        remaining = self.state.quota.remaining(cap)
        if remaining <= 0:
            raise StopRun(
                f"daily audio cap reached ({self.state.quota.used}/{cap} on "
                f"{self.state.quota.date}). Remaining episodes stay queued for the next run.",
                EXIT_STOPPED_EARLY,
            )

        # Mark intent BEFORE the call: if we die mid-flight, the next run sees
        # STATUS_GENERATING with no task_id and adopts rather than regenerates.
        self.state.quota.consume(cap)
        record.status = STATUS_GENERATING
        record.attempts += 1
        record.touch()
        self.state.save()
        self.generated_this_run.add(episode.key)
        self.log.info("generating audio (quota %d/%d today, attempt %d)",
                      self.state.quota.used, cap, record.attempts)

        try:
            task_id = self.cli.start_audio(
                record.notebook_id,
                episode.focus,
                audio_format=self.cfg.generate.format,
                length=self.cfg.generate.length,
                retry=self.cfg.generate.retry,
            )
        except NotebookLMError as exc:
            self._unwind_failed_start(record, exc)
            raise

        record.task_id = task_id
        record.touch()
        self.state.save()
        self.log.info("generation started: task %s", task_id)
        return self._await_task(record)

    def _unwind_failed_start(self, record: EpisodeRecord, exc: NotebookLMError) -> None:
        """Decide what a failed `generate audio` did to the server, then record it.

        The debit-first ordering protects against a crash *after* the server
        accepted the job. When the call itself raises we can often do better:

        * RATE_LIMITED - the server refused outright, so nothing was generated.
          Refund the unit and put the episode back in a startable state,
          otherwise a transient limit wedges it in `generating` with no task_id
          and the next run has nothing to adopt.
        * "Audio generation is unavailable" - same thing: a synchronous refusal
          because the sources are still ingesting. Refund and rewind, but the
          run continues (unlike a rate limit, this says nothing about the other
          episodes, which have their own notebooks).
        * Anything ambiguous (local timeout, unparseable response, network drop)
          - the request may well have reached NotebookLM. Keep the debit and the
          `generating` state so the next run adopts the artifact instead of
          paying twice.
        """
        if exc.is_generation_unavailable:
            self.state.quota.used = max(0, self.state.quota.used - 1)
            record.status = STATUS_SOURCES_ADDED
            record.attempts = max(0, record.attempts - 1)
            record.last_error = scrub_text(
                f"NotebookLM refused to start generation ({exc.message}). This normally means "
                f"the sources were still ingesting. Nothing was generated; the quota unit was "
                f"refunded and the next run retries."
            )
            self.log.warning(
                "generation refused as unavailable (sources likely still ingesting); refunded "
                "the quota unit (now %d/%d today) and left %s ready to retry",
                self.state.quota.used, self.cfg.daily_audio_cap, record.slug,
            )
            record.touch()
            self.state.save()
            return

        if exc.is_rate_limit:
            self.state.quota.used = max(0, self.state.quota.used - 1)
            record.status = STATUS_SOURCES_ADDED
            record.attempts = max(0, record.attempts - 1)
            record.last_error = scrub_text(f"rate limited before generation started: {exc.message}")
            # generated_this_run is deliberately NOT cleared: the episode is
            # startable again on the *next* run, never twice within this one.
            self.log.warning(
                "rate limited before generation started; refunded the quota unit "
                "(now %d/%d today) and left %s ready to retry on the next run",
                self.state.quota.used, self.cfg.daily_audio_cap, record.slug,
            )
        else:
            record.last_error = scrub_text(
                f"generate audio failed ambiguously ({exc.code}): {exc.message}. The request may "
                f"have reached NotebookLM, so the next run will look for an existing artifact "
                f"before generating again."
            )
            self.log.error(
                "generate audio failed (%s) with the outcome unknown; keeping the quota debit "
                "and leaving %s for adoption on the next run", exc.code, record.slug,
            )
        record.touch()
        self.state.save()

    def _await_task(self, record: EpisodeRecord) -> GenerationStatus:
        """Poll once, then block up to the configured timeout. A timeout leaves
        the record resumable - it never counts as a failure."""
        assert record.notebook_id and record.task_id

        status = self.cli.poll(record.notebook_id, record.task_id)
        if not status.is_complete and not status.is_failed:
            self.log.info("waiting for generation (timeout %ds)", self.cfg.generate.timeout)
            status = self.cli.wait(
                record.notebook_id,
                record.task_id,
                timeout=self.cfg.generate.timeout,
                interval=self.cfg.generate.interval,
            )

        if status.is_complete:
            record.status = STATUS_GENERATED
            record.artifact = {"url": status.url, "task_id": status.task_id}
            record.last_error = None
            record.touch()
            self.state.save()
            self.log.info("generation complete")
            return status

        if status.is_failed:
            record.status = STATUS_FAILED
            record.last_error = scrub_text(status.error) or "generation failed server-side"
            record.touch()
            self.state.save()
            self.log.error("generation FAILED: %s", record.last_error)
            return status

        self.log.warning(
            "generation still running after %ds - leaving task %s pending. The next run polls "
            "it; do NOT regenerate.", self.cfg.generate.timeout, record.task_id,
        )
        record.touch()
        self.state.save()
        return status

    def _download_and_deliver(self, record: EpisodeRecord, episode: QueuedEpisode) -> str:
        assert record.notebook_id
        tmp_path = temp_target(self.cfg.tmp_dir, episode.slug)
        self.log.info("downloading audio -> %s", tmp_path)
        downloaded = self.cli.download_audio(record.notebook_id, tmp_path)

        try:
            result = deliver(
                downloaded,
                slug=episode.slug,
                inbox_dir=self.cfg.inbox_dir,
                quarantine_dir=self.cfg.quarantine_dir,
                ffprobe_bin=self.cfg.ffprobe_bin,
                min_duration_sec=self.cfg.min_duration_sec,
                sidecar=episode.sidecar,
                log=self.log,
            )
        except InboxOccupiedError as exc:
            # Nothing is wrong with the audio, so it stays in tmp/ exactly where
            # the message says. The record stays `generated`, so clearing the
            # inbox and re-running delivers it without touching the network.
            record.status = STATUS_GENERATED
            record.last_error = scrub_text(str(exc))
            record.touch()
            self.state.save()
            self.log.error("%s: %s", episode.slug, record.last_error)
            return OUTCOME_FAILED
        except DeliveryError as exc:
            if downloaded.exists():  # not quarantined by deliver(); park it safely
                quarantine(downloaded, self.cfg.quarantine_dir, str(exc), self.log)
            record.status = STATUS_QUARANTINED
            record.last_error = scrub_text(str(exc))
            record.touch()
            self.state.save()
            return OUTCOME_QUARANTINED

        record.status = STATUS_DELIVERED
        record.output_path = str(result.audio_path)
        record.artifact.update(result.verification.as_dict())
        record.sidecar = SIDECAR_PRESENT if result.sidecar_path else SIDECAR_MISSING
        record.last_error = None
        record.touch()
        self.state.save()
        return OUTCOME_DELIVERED

    def _fail(self, episode: QueuedEpisode, message: str) -> None:
        message = scrub_text(message)
        record = self.state.get(episode.key)
        if record is not None:
            record.last_error = message
            record.touch()
        self.outcomes[episode.slug] = OUTCOME_FAILED
        self.log.error("%s: %s", episode.slug, message)

    def _summarize(self) -> None:
        self.log.info("")
        self.log.info("=== Summary ===")
        for slug, outcome in self.outcomes.items():
            self.log.info("  %-12s %s", outcome, slug)
        cap = self.cfg.daily_audio_cap
        self.log.info("quota: %d/%d used today (%s)",
                      self.state.quota.used, cap, self.state.quota.date)
        if any(o == OUTCOME_DELIVERED for o in self.outcomes.values()):
            self.log.info("Next: follow the publish workflow in CLAUDE.md "
                          "(.venv/bin/python podpub.py --dry-run first).")


# ---------- dry run ----------

def print_plan(cfg: Config, state: StateStore, queued: list[QueuedEpisode],
               log: logging.Logger) -> int:
    """Describe what `run` would do. Makes no network calls."""
    log.info("=== Dry run: no network calls, no state writes ===")
    log.info("queue:      %s", cfg.queue_dir)
    log.info("inbox:      %s", cfg.inbox_dir)
    log.info("quota:      %d/%d used today (%s)",
             state.quota.used, cfg.daily_audio_cap, state.quota.date)
    if not queued:
        log.info("(queue is empty)")
        return EXIT_OK

    budget = state.quota.remaining(cfg.daily_audio_cap)
    seen_keys: set[str] = set()
    for episode in queued:
        record = state.get(episode.key)
        status = record.status if record else "new"
        duplicate = episode.key in seen_keys
        seen_keys.add(episode.key)
        log.info("")
        log.info("- %s  [%s]", episode.title, status)
        log.info("    key:      %s", episode.key[:12])
        log.info("    folder:   %s", episode.directory)
        log.info("    pdfs:     %s", ", ".join(p.name for p in episode.pdfs))
        log.info("    sidecar:  %s", episode.sidecar.name if episode.sidecar else "MISSING (write one!)")
        log.info("    focus:    %s", _ellipsize(episode.focus, 100))
        log.info("    delivers: %s", cfg.inbox_dir / f"{episode.slug}.m4a")

        if duplicate:
            log.info("    action:   SKIP - same PDF content as an earlier folder in this queue")
        elif record and record.is_delivered:
            log.info("    action:   skip (already delivered)")
        elif record and record.status in (STATUS_QUARANTINED, STATUS_FAILED):
            log.info("    action:   skip (%s: %s)", record.status, record.last_error)
            log.info("              needs a human: see automation/INSTRUCTIONS.md "
                     "'Failure states in detail'")
        # The three branches below must not use the word "generate": the plan is
        # the agent's contract that no new audio - and no quota - is at stake.
        elif record and record.status == STATUS_GENERATED:
            log.info("    action:   download + verify + deliver (no new audio, no quota cost)")
        elif record and record.task_id:
            log.info("    action:   poll existing task %s (resumes the job already paid for; "
                     "no quota cost)", record.task_id)
        elif record and record.status == STATUS_GENERATING:
            # Debited but no task_id: the run adopts an existing artifact, or
            # fails this one episode. It never starts new audio.
            log.info("    action:   adopt the existing artifact in notebook %s, or fail this "
                     "episode (no quota cost)", record.notebook_id)
        elif budget <= 0:
            log.info("    action:   defer (daily cap %d reached)", cfg.daily_audio_cap)
        else:
            budget -= 1
            log.info("    action:   create notebook, add %d source(s), generate %s/%s "
                     "(1 quota unit, %d left after)",
                     len(episode.pdfs), cfg.generate.format, cfg.generate.length, budget)
    return EXIT_OK


def _ellipsize(text: str, width: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


# ---------- status ----------

def print_status(cfg: Config, state: StateStore, cli: NotebookLMCLI,
                 log: logging.Logger) -> int:
    log.info("=== notebooklm pipeline status ===")
    try:
        version = cli.version() or "unknown"
        if version_matches(version):
            log.info("cli:     %s", version)
        else:
            log.warning("cli:     %s - MISMATCH, `run` is pinned to %s and will refuse to start",
                        version, VERIFIED_CLI_VERSION)
    except NotebookLMNotInstalled as exc:
        log.error("cli:     %s", exc)
    log.info("state:   %s", state.path)
    log.info("quota:   %d/%d used today (%s), %d remaining",
             state.quota.used, cfg.daily_audio_cap, state.quota.date,
             state.quota.remaining(cfg.daily_audio_cap))

    lock = read_lock(cfg.lock_file)
    log.info("lock:    %s", lock.describe() if lock else "free")

    queued = episodes_mod.scan_queue(cfg.queue_dir, log)
    log.info("queue:   %d folder(s) in %s", len(queued), cfg.queue_dir)
    for episode in queued:
        record = state.get(episode.key)
        log.info("  %-40s %-16s %s", _ellipsize(episode.title, 40),
                 record.status if record else "new",
                 "sidecar ok" if episode.has_sidecar else "SIDECAR MISSING")

    tracked = [r for r in state.episodes.values() if r.status != STATUS_DELIVERED]
    if tracked:
        log.info("in flight:")
        for record in tracked:
            log.info("  %-40s %-16s task=%s %s", _ellipsize(record.title, 40), record.status,
                     record.task_id or "-", f"err={record.last_error}" if record.last_error else "")
    return EXIT_OK


# ---------- entry point ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nlm_pipeline",
        description="Generate NotebookLM Deep Dives and deliver them to podpub's inbox.",
    )
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging on stdout")
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="process the watch folder")
    run_cmd.add_argument("--dry-run", action="store_true",
                         help="print the plan; makes no network calls and writes no state")
    run_cmd.add_argument("--limit", type=int, default=None,
                         help="process at most N queued episodes this run")

    sub.add_parser("status", help="show state, quota, lock, and queue")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    # `status` and `--dry-run` are read-only: no directories, no log file.
    read_only = args.command == "status" or getattr(args, "dry_run", False)
    if not read_only:
        cfg.ensure_dirs()
    log = setup_logging(cfg.log_file, args.verbose, to_file=not read_only)
    cli = NotebookLMCLI(binary=cfg.notebooklm_bin, profile=cfg.profile,
                        timeout=cfg.command_timeout, log=logging.getLogger("nlm.cli"))

    try:
        state = StateStore.load(cfg.state_file)
    except StateCorruptError as exc:
        log.error("state error: %s", exc)
        return EXIT_CONFIG_ERROR
    state.quota.rollover()

    if args.command == "status":
        return print_status(cfg, state, cli, log)

    queued = episodes_mod.scan_queue(cfg.queue_dir, log)

    if args.dry_run:
        return print_plan(cfg, state, queued, log)

    try:
        with PipelineLock(cfg.lock_file) as lock:
            if lock.reclaimed_stale:
                log.warning("reclaimed a stale lockfile (previous run died)")
            pipeline = Pipeline(cfg, cli, state, log, limit=args.limit)
            return pipeline.run(queued)
    except LockBusyError as exc:
        log.error("%s", exc)
        return EXIT_LOCK_BUSY
    except NotebookLMNotInstalled as exc:
        log.error("%s", exc)
        return EXIT_CONFIG_ERROR
    except StopRun as exc:
        log.error("%s", exc)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
