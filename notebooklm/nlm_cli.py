#!/usr/bin/env python3
"""Subprocess wrapper around the external `notebooklm` CLI (notebooklm-py).

This is the only module in the package that talks to the network-facing tool.
Everything else in the pipeline depends on the small typed surface at the
bottom of this file, which makes the whole pipeline mockable in tests.

Rules enforced here:
  * argv is always a list - never `shell=True`, never string interpolation.
  * `--json` is always requested; stdout is parsed as JSON.
  * stdout/stderr are scrubbed (see scrub.py) before they reach a log.
  * we never pass, read, or store credentials - auth lives inside
    notebooklm-py's own profile storage.

CLI CONTRACT (verified against notebooklm-py 0.8.0, commit 8fb61cb1, via
`notebooklm <cmd> --help` and the installed source). If you upgrade the tool,
re-verify this section first - it is the only place CLI strings appear.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .scrub import scrub_args, scrub_text

# ---------- CLI contract ----------

VERIFIED_CLI_VERSION = "0.8.0"
PINNED_INSTALL_SPEC = f'"notebooklm-py[browser]=={VERIFIED_CLI_VERSION}"'

# Global options
OPT_PROFILE = "-p"
OPT_JSON = "--json"
OPT_NOTEBOOK = "-n"

# `--` terminates option parsing so a title/path/id beginning with "-" is taken
# as a positional. Click requires every option BEFORE it: args after `--` are
# all positional, so a trailing `--json` would be rejected as an extra argument.
END_OPTS = "--"

# Subcommands (argv fragments only - ids/paths/prompts are appended at runtime)
CMD_VERSION = ["--version"]
CMD_AUTH_CHECK = ["auth", "check", "--test", "--passive", OPT_JSON]
CMD_AUTH_REFRESH = ["auth", "refresh", "--verify", "--quiet", OPT_JSON]
CMD_CREATE = ["create"]                       # + [--json, --, TITLE]
CMD_SOURCE_ADD = ["source", "add"]            # + [-n ID, --type file, --json, --, PATH]
CMD_SOURCE_LIST = ["source", "list"]          # + [-n ID, --json]
CMD_GENERATE_AUDIO = ["generate", "audio"]    # + [-n ID, --format, --length, --no-wait, --retry, --prompt-file F, --json]
CMD_ARTIFACT_POLL = ["artifact", "poll"]      # + [-n ID, --json, --, TASK_ID]
CMD_ARTIFACT_WAIT = ["artifact", "wait"]      # + [-n ID, --timeout N, --interval N, --json, --, TASK_ID]
CMD_ARTIFACT_LIST = ["artifact", "list"]      # + [-n ID, --type audio, --json]
CMD_DOWNLOAD_AUDIO = ["download", "audio"]    # + [-n ID, --latest, --force, --json, --, OUTPUT_PATH]

OPT_SOURCE_TYPE_FILE = ["--type", "file"]
OPT_ARTIFACT_TYPE_AUDIO = ["--type", "audio"]
OPT_NO_WAIT = "--no-wait"
OPT_FORMAT = "--format"
OPT_LENGTH = "--length"
OPT_RETRY = "--retry"
OPT_TIMEOUT = "--timeout"
OPT_INTERVAL = "--interval"
OPT_LATEST = "--latest"
OPT_FORCE = "--force"
# The focus prompt goes through a file, never argv: a prompt starting with "-"
# would otherwise be parsed as an option, and prompts can be long.
OPT_PROMPT_FILE = "--prompt-file"

# Generation states reported by `artifact poll` / `artifact wait`. Only
# `completed` and `failed` are terminal; everything else (pending, timeout,
# in-progress, anything Google adds later) means "not done yet, come back".
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_PENDING = "pending"
STATUS_TIMEOUT = "timeout"          # emitted by `artifact wait` on its own timeout
TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED})

# Source ingestion states from `source list --json` (rpc/types.py SourceStatus:
# processing / ready / error / preparing, plus "unknown" for future codes).
# NotebookLM refuses to generate audio until every source is `ready`.
SOURCE_READY = "ready"
SOURCE_ERROR = "error"

# NotebookLM's synchronous refusal when sources are still ingesting. Observed on
# the first real run: 3 PDFs (34MB) uploaded, generate fired immediately, server
# answered "Error: Audio generation is unavailable"; ~90s later all sources read
# `ready` and the same call succeeded. Nothing was generated, so this is safe to
# treat as "did not start" and refund.
GENERATION_UNAVAILABLE_MARKER = "audio generation is unavailable"

# Error codes emitted in the `{"error": true, "code": ...}` envelope.
CODE_RATE_LIMITED = "RATE_LIMITED"
CODE_AUTH = "AUTH_ERROR"
CODE_NETWORK = "NETWORK_ERROR"
CODE_CANCELLED = "CANCELLED"
CODE_CLI = "CLI_ERROR"
CODE_TIMEOUT = "LOCAL_TIMEOUT"
CODE_BAD_JSON = "BAD_JSON"

INSTALL_HINT = (
    "notebooklm CLI not found. Install the pinned version with:\n"
    f"  uv tool install --python 3.13 {PINNED_INSTALL_SPEC}\n"
    "then authenticate once with: notebooklm login"
)


def version_matches(version_text: str) -> bool:
    """True when the installed CLI is the version this wrapper was verified against.

    Guessing at an unverified CLI's output can cost a quota unit (a mis-parsed
    generate response looks like "no task_id" and invites a regenerate), so the
    pipeline refuses to run on a version it has not been checked against.
    """
    return VERIFIED_CLI_VERSION in (version_text or "")


# ---------- errors ----------

class NotebookLMError(RuntimeError):
    """A `notebooklm` invocation failed. `code` mirrors the CLI's error code."""

    def __init__(self, code: str, message: str, *, args_repr: str = "") -> None:
        super().__init__(f"[{code}] {message}" + (f" (while running: {args_repr})" if args_repr else ""))
        self.code = code
        self.message = message

    @property
    def is_rate_limit(self) -> bool:
        return self.code == CODE_RATE_LIMITED

    @property
    def is_auth(self) -> bool:
        return self.code == CODE_AUTH

    @property
    def is_generation_unavailable(self) -> bool:
        """NotebookLM refused to start audio generation (sources still ingesting).

        Matched on the message rather than a code: the CLI reports it as a
        generic NOTEBOOKLM_ERROR, so the wording is the only signal.
        """
        return GENERATION_UNAVAILABLE_MARKER in self.message.lower()


class NotebookLMNotInstalled(NotebookLMError):
    def __init__(self, binary: str) -> None:
        super().__init__("NOT_INSTALLED", f"{binary!r} is not on PATH. {INSTALL_HINT}")


# ---------- typed results ----------

@dataclass(frozen=True)
class GenerationStatus:
    """Normalized view of `artifact poll` / `artifact wait` output."""

    task_id: str | None
    status: str
    url: str | None = None
    error: str | None = None
    error_code: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.status == STATUS_COMPLETED

    @property
    def is_failed(self) -> bool:
        return self.status == STATUS_FAILED


@dataclass
class NotebookLMCLI:
    """Invokes the `notebooklm` binary and returns parsed JSON.

    Every method is a single subprocess call. Tests substitute a fake with the
    same method names (see notebooklm/tests/fakes.py).
    """

    binary: str = "notebooklm"
    profile: str | None = None
    timeout: int = 120
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("nlm.cli"))

    # ----- plumbing -----

    def _argv(self, tail: list[str]) -> list[str]:
        head = [self.binary]
        if self.profile:
            head += [OPT_PROFILE, self.profile]
        return head + tail

    def _run(self, tail: list[str], *, timeout: int | None = None,
             status_payload: bool = False) -> Any:
        """Run one command and return its parsed JSON.

        `status_payload=True` is for `artifact poll` / `artifact wait`, whose
        JSON is a *status report*, not a result-or-error envelope:

          * Both always include an "error" key - null on success, a string when
            the generation failed or the wait timed out. Treating that string as
            a failure envelope turns the two most expected outcomes of a 10-20
            minute Deep Dive into "[CLI_ERROR] unknown error".
          * `wait` exits 1 for ANY non-completed status, including plain
            pending, so a non-zero exit is not evidence of a problem either.

        For those two commands only, an error envelope is recognized by
        `error is True`. Everywhere else the truthy check stays: `download`'s
        legacy shape genuinely is `{"error": "<message>"}`.
        """
        argv = self._argv(tail)
        self.log.debug("exec: %s", " ".join(scrub_args(argv)))
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise NotebookLMNotInstalled(self.binary) from exc
        except subprocess.TimeoutExpired as exc:
            raise NotebookLMError(
                CODE_TIMEOUT,
                f"no response after {timeout or self.timeout}s",
                args_repr=" ".join(scrub_args(tail)),
            ) from exc

        stderr = scrub_text(proc.stderr).strip()
        if stderr:
            self.log.debug("stderr: %s", stderr)

        payload = _parse_json(proc.stdout)
        is_status_report = status_payload and isinstance(payload, dict) and "status" in payload

        if isinstance(payload, dict) and _is_error_envelope(payload, strict=status_payload):
            raise NotebookLMError(
                str(payload.get("code") or CODE_CLI),
                scrub_text(str(payload.get("message") or "unknown error")),
                args_repr=" ".join(scrub_args(tail)),
            )
        if proc.returncode != 0 and not is_status_report:
            detail = stderr or scrub_text(proc.stdout).strip() or f"exit {proc.returncode}"
            raise NotebookLMError(CODE_CLI, detail, args_repr=" ".join(scrub_args(tail)))
        if payload is None:
            raise NotebookLMError(
                CODE_BAD_JSON,
                f"expected JSON on stdout, got: {scrub_text(proc.stdout)[:200]!r}",
                args_repr=" ".join(scrub_args(tail)),
            )
        return payload

    # ----- auth (read-only probes; login is always a manual human step) -----

    def version(self) -> str:
        argv = self._argv(CMD_VERSION)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
        except FileNotFoundError as exc:
            raise NotebookLMNotInstalled(self.binary) from exc
        return scrub_text(proc.stdout).strip()

    def auth_ok(self) -> tuple[bool, str]:
        """Passive readiness probe. Returns (ok, detail). Never mutates cookies.

        A missing binary is NOT an auth problem and must not be reported as one -
        it propagates so the caller can tell the user to install the tool rather
        than to log in again.
        """
        try:
            self._run(CMD_AUTH_CHECK, timeout=60)
        except NotebookLMNotInstalled:
            raise
        except NotebookLMError as exc:
            return False, exc.message
        return True, "authenticated"

    def auth_refresh(self) -> tuple[bool, str]:
        """One-shot cookie keepalive. Returns (ok, detail)."""
        try:
            self._run(CMD_AUTH_REFRESH, timeout=120)
        except NotebookLMNotInstalled:
            raise
        except NotebookLMError as exc:
            return False, exc.message
        return True, "refreshed"

    # ----- notebooks / sources -----

    def create_notebook(self, title: str) -> str:
        payload = self._run(CMD_CREATE + [OPT_JSON, END_OPTS, title])
        notebook_id = _dig(payload, ("notebook", "id")) or payload.get("id")
        if not notebook_id:
            raise NotebookLMError(CODE_BAD_JSON, f"no notebook id in create response: {payload!r}")
        return str(notebook_id)

    def add_source(self, notebook_id: str, pdf: Path, *, timeout: int = 300) -> str:
        payload = self._run(
            CMD_SOURCE_ADD
            + [OPT_NOTEBOOK, notebook_id, *OPT_SOURCE_TYPE_FILE, OPT_JSON, END_OPTS, str(pdf)],
            timeout=timeout,
        )
        source_id = _dig(payload, ("source", "id")) or payload.get("source_id")
        return str(source_id) if source_id else ""

    def list_sources(self, notebook_id: str) -> list[dict[str, Any]]:
        """Sources with their ingestion `status` (ready / processing / ...)."""
        payload = self._run(
            CMD_SOURCE_LIST + [OPT_NOTEBOOK, notebook_id, OPT_JSON],
            timeout=90,
        )
        if isinstance(payload, list):
            return payload
        sources = payload.get("sources")
        return sources if isinstance(sources, list) else []

    # ----- generation -----

    def start_audio(
        self,
        notebook_id: str,
        prompt: str,
        *,
        audio_format: str = "deep-dive",
        length: str = "long",
        retry: int = 3,
    ) -> str:
        """Kick off a Deep Dive and return the task_id WITHOUT blocking.

        We deliberately do not use `--wait` here: the task_id must reach
        state.json before any long block, so a crashed or killed run resumes by
        polling instead of burning a second quota unit on a regenerate.
        """
        with tempfile.TemporaryDirectory(prefix="nlm-prompt-") as tmpdir:
            prompt_file = Path(tmpdir) / "focus.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            payload = self._run(
                CMD_GENERATE_AUDIO
                + [
                    OPT_NOTEBOOK, notebook_id,
                    OPT_FORMAT, audio_format,
                    OPT_LENGTH, length,
                    OPT_NO_WAIT,
                    OPT_RETRY, str(retry),
                    OPT_PROMPT_FILE, str(prompt_file),
                    OPT_JSON,
                ],
                timeout=180,
            )
        task_id = payload.get("task_id")
        if not task_id:
            raise NotebookLMError(CODE_BAD_JSON, f"no task_id in generate response: {payload!r}")
        return str(task_id)

    def poll(self, notebook_id: str, task_id: str) -> GenerationStatus:
        payload = self._run(
            CMD_ARTIFACT_POLL + [OPT_NOTEBOOK, notebook_id, OPT_JSON, END_OPTS, task_id],
            timeout=90,
            status_payload=True,
        )
        return _to_status(payload, task_id)

    def wait(self, notebook_id: str, task_id: str, *, timeout: int, interval: int = 10) -> GenerationStatus:
        """Block until terminal or `timeout`. A timeout is NOT an error here -
        it returns a pending status so the caller can resume on the next run."""
        try:
            payload = self._run(
                CMD_ARTIFACT_WAIT
                + [
                    OPT_NOTEBOOK, notebook_id,
                    OPT_TIMEOUT, str(timeout),
                    OPT_INTERVAL, str(interval),
                    OPT_JSON,
                    END_OPTS, task_id,
                ],
                timeout=timeout + 120,
                status_payload=True,
            )
        except NotebookLMError as exc:
            if exc.code in (CODE_TIMEOUT, CODE_CANCELLED):
                return GenerationStatus(task_id=task_id, status=STATUS_PENDING, error=exc.message)
            raise
        return _to_status(payload, task_id)

    def list_audio_artifacts(self, notebook_id: str) -> list[dict[str, Any]]:
        payload = self._run(
            CMD_ARTIFACT_LIST + [OPT_NOTEBOOK, notebook_id, *OPT_ARTIFACT_TYPE_AUDIO, OPT_JSON],
            timeout=90,
        )
        if isinstance(payload, list):
            return payload
        artifacts = payload.get("artifacts")
        return artifacts if isinstance(artifacts, list) else []

    # ----- download -----

    def download_audio(self, notebook_id: str, dest: Path, *, timeout: int = 900) -> Path:
        payload = self._run(
            CMD_DOWNLOAD_AUDIO
            + [OPT_NOTEBOOK, notebook_id, OPT_LATEST, OPT_FORCE, OPT_JSON, END_OPTS, str(dest)],
            timeout=timeout,
        )
        written = payload.get("output_path") if isinstance(payload, dict) else None
        if written:
            # Never follow the third-party CLI to a path we did not ask for: the
            # caller goes on to move this file into podpub's inbox, so a
            # surprising output_path would relocate an arbitrary file.
            if Path(written).resolve() != dest.resolve():
                raise NotebookLMError(
                    CODE_CLI,
                    f"download wrote to {written!r} instead of the requested {str(dest)!r}; "
                    f"refusing to touch it",
                )
        if not dest.exists():
            raise NotebookLMError(CODE_CLI, f"download reported success but {dest} is missing")
        return dest


# ---------- parsing helpers ----------

def _is_error_envelope(payload: dict, *, strict: bool) -> bool:
    """Whether a payload is the CLI's `{"error": ..., "code": ...}` failure shape.

    `strict` (poll/wait) demands the literal `True` the envelope always carries,
    so a status report's descriptive `"error": "<reason>"` string is left alone.
    """
    error = payload.get("error")
    return error is True if strict else bool(error)


def _parse_json(stdout: str) -> Any:
    """Parse a JSON document out of stdout, tolerating leading prose lines."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        return None
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        return None


def _dig(payload: Any, path: tuple[str, ...]) -> Any:
    node = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _to_status(payload: Any, task_id: str) -> GenerationStatus:
    """Normalize a poll/wait payload into a GenerationStatus.

    Two shapes feed in: `poll` keys the id as `task_id`, `wait` as
    `artifact_id`. Neither name is required here - the `task_id` we were called
    with is the fallback, and it is the same identifier either way.

    Any status outside TERMINAL_STATUSES collapses to `pending`, which is what
    makes a `wait` timeout resumable instead of fatal: the job keeps running
    server-side, so the next run polls it rather than paying to regenerate.
    """
    if not isinstance(payload, dict):
        return GenerationStatus(task_id=task_id, status=STATUS_PENDING)

    raw_status = str(payload.get("status") or STATUS_PENDING)
    status = raw_status if raw_status in TERMINAL_STATUSES else STATUS_PENDING

    # The url is only ever stored in state.json (downloads go through the CLI),
    # so scrub it: artifact URLs can carry signed-token query strings.
    return GenerationStatus(
        task_id=str(payload.get("task_id") or payload.get("artifact_id") or task_id),
        status=status,
        url=scrub_text(payload["url"]) if payload.get("url") else None,
        error=scrub_text(payload["error"]) if payload.get("error") else None,
        error_code=payload.get("error_code"),
    )
