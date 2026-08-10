"""Test doubles: a fake `notebooklm` CLI and helpers for building queues.

FakeCLI records every call so tests can assert on what the pipeline *didn't*
do - the important assertions here are negative (no second generate, no
network in --dry-run).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from notebooklm.config import Config
from notebooklm.nlm_cli import GenerationStatus, NotebookLMError


def silent_logger(name: str = "nlm.test") -> logging.Logger:
    log = logging.getLogger(name)
    log.handlers.clear()
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


class FakeCLI:
    """Mimics NotebookLMCLI's surface without touching the network."""

    def __init__(self, *, auth: bool = True, refresh_ok: bool = True,
                 statuses: list[str] | None = None,
                 artifacts: list[dict[str, Any]] | None = None,
                 download_bytes: bytes = b"fake-audio",
                 raise_on: dict[str, Exception] | None = None,
                 source_statuses: list[list[str]] | None = None) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.auth = auth
        self.refresh_ok = refresh_ok
        # Consumed one per poll/wait; the last value repeats.
        self.statuses = statuses or ["completed"]
        # One entry per list_sources call (statuses of each source); last repeats.
        # Default: everything is ready immediately, so existing tests are unaffected.
        self.source_statuses = source_statuses or [["ready"]]
        self.artifacts = artifacts or []
        self.download_bytes = download_bytes
        self.raise_on = raise_on or {}
        self.notebook_seq = 0
        self.task_seq = 0

    # ----- bookkeeping -----

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if name in self.raise_on:
            raise self.raise_on[name]

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def count(self, name: str) -> int:
        return self.names().count(name)

    def _next_status(self) -> str:
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    # ----- CLI surface -----

    def version(self) -> str:
        self._record("version")
        return "NotebookLM CLI, version 0.8.0 (fake)"

    def auth_ok(self) -> tuple[bool, str]:
        self._record("auth_ok")
        return (True, "authenticated") if self.auth else (False, "cookies expired")

    def auth_refresh(self) -> tuple[bool, str]:
        self._record("auth_refresh")
        if self.refresh_ok:
            self.auth = True
            return True, "refreshed"
        return False, "refresh failed"

    def create_notebook(self, title: str) -> str:
        self._record("create_notebook", title)
        self.notebook_seq += 1
        return f"nb_{self.notebook_seq:03d}"

    def add_source(self, notebook_id: str, pdf: Path, *, timeout: int = 300) -> str:
        self._record("add_source", notebook_id, pdf)
        return f"src_{Path(pdf).stem}"

    def list_sources(self, notebook_id: str) -> list[dict[str, Any]]:
        self._record("list_sources", notebook_id)
        statuses = (self.source_statuses.pop(0) if len(self.source_statuses) > 1
                    else self.source_statuses[0])
        return [{"id": f"src_{i}", "title": f"paper{i}.pdf", "status": st}
                for i, st in enumerate(statuses)]

    def start_audio(self, notebook_id: str, prompt: str, **kwargs: Any) -> str:
        self._record("start_audio", notebook_id, prompt, **kwargs)
        self.task_seq += 1
        return f"task_{self.task_seq:03d}"

    def poll(self, notebook_id: str, task_id: str) -> GenerationStatus:
        self._record("poll", notebook_id, task_id)
        return self._status(task_id, self._next_status())

    def wait(self, notebook_id: str, task_id: str, *, timeout: int,
             interval: int = 10) -> GenerationStatus:
        self._record("wait", notebook_id, task_id, timeout=timeout)
        return self._status(task_id, self._next_status())

    def list_audio_artifacts(self, notebook_id: str) -> list[dict[str, Any]]:
        self._record("list_audio_artifacts", notebook_id)
        return list(self.artifacts)

    def download_audio(self, notebook_id: str, dest: Path, *, timeout: int = 900) -> Path:
        self._record("download_audio", notebook_id, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.download_bytes)
        return dest

    def _status(self, task_id: str, status: str) -> GenerationStatus:
        return GenerationStatus(
            task_id=task_id,
            status=status,
            url=f"https://example.invalid/{task_id}.m4a" if status == "completed" else None,
            error="server-side failure" if status == "failed" else None,
        )


def rate_limit_error() -> NotebookLMError:
    return NotebookLMError("RATE_LIMITED", "Error: Rate limited. Retry after 600s.")


def auth_error() -> NotebookLMError:
    return NotebookLMError("AUTH_ERROR", "Authentication error: cookies expired")


def generation_unavailable_error() -> NotebookLMError:
    """Verbatim shape of the refusal seen on the first real run."""
    return NotebookLMError("NOTEBOOKLM_ERROR", "Error: Audio generation is unavailable")


def make_config(root: Path, **overrides: Any) -> Config:
    """A Config rooted in a tmp dir, with every directory created."""
    cfg = Config.defaults(root)
    cfg.inbox_dir = root / "inbox"
    for key, value in overrides.items():
        setattr(cfg, key, value)
    cfg.ensure_dirs()
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def make_episode_dir(queue_dir: Path, name: str, *, pdfs: dict[str, bytes] | None = None,
                     title: str | None = None, focus: str | None = None,
                     sidecar: str | None = None, sidecar_name: str | None = None) -> Path:
    """Create a queue/<name>/ folder with PDFs and optional metadata files."""
    directory = queue_dir / name
    directory.mkdir(parents=True, exist_ok=True)
    for filename, content in (pdfs or {"paper.pdf": b"%PDF-1.4 fake"}).items():
        (directory / filename).write_bytes(content)
    if title is not None:
        (directory / "title.txt").write_text(title + "\n", encoding="utf-8")
    if focus is not None:
        (directory / "focus.txt").write_text(focus + "\n", encoding="utf-8")
    if sidecar is not None:
        name_md = sidecar_name or "episode.md"
        (directory / name_md).write_text(sidecar, encoding="utf-8")
    return directory
