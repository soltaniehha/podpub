#!/usr/bin/env python3
"""Single-instance lockfile.

Two concurrent pipeline runs would double-spend the audio quota and race on
state.json. The lock also stands in for "don't run while podpub.py is running"
- that rule is enforced by convention (see automation/INSTRUCTIONS.md), since
podpub.py predates this package and is not to be modified.

Acquisition is a single `open(O_CREAT|O_EXCL)`, which is atomic even when two
processes start at the same instant. There is deliberately no write-then-rename
here: a shared temp path made the loser of a race crash instead of reporting
"busy".

Google Drive syncs this directory, which shapes two rules:
  * A lockfile we cannot parse is treated as HELD, not stale - a partially
    synced file is more likely to be a live run than a dead one.
  * A lockfile written by a different host is always HELD. We cannot ask
    another machine whether its PID is alive, and PID numbers are not
    comparable across machines.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType

LOCK_MODE = 0o600


class LockBusyError(RuntimeError):
    """Another pipeline run holds the lock."""


@dataclass(frozen=True)
class LockInfo:
    pid: int
    host: str
    started_at: str
    age_sec: float
    parsed: bool = True

    @property
    def same_host(self) -> bool:
        return self.host == socket.gethostname()

    @property
    def is_live(self) -> bool:
        """Whether the holding process still exists. Only meaningful locally."""
        return self.same_host and _pid_alive(self.pid)

    def describe(self) -> str:
        if not self.parsed:
            return "held (lockfile unreadable - assuming a live run)"
        if not self.same_host:
            return f"held by pid {self.pid} on another host ({self.host})"
        return (f"held by pid {self.pid} on {self.host} since {self.started_at} "
                f"({'RUNNING' if self.is_live else 'STALE - will be reclaimed'})")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    return True


def read_lock(path: Path) -> LockInfo | None:
    """Inspect the lockfile without acquiring it. None when the lock is free."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return LockInfo(pid=-1, host="?", started_at="?", age_sec=0.0, parsed=False)

    try:
        data = json.loads(raw)
        pid = int(data["pid"])
        host = str(data["host"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return LockInfo(pid=-1, host="?", started_at="?", age_sec=0.0, parsed=False)

    try:
        age = max(0.0, datetime.now().timestamp() - path.stat().st_mtime)
    except OSError:
        age = 0.0
    return LockInfo(pid=pid, host=host,
                    started_at=str(data.get("started_at") or "?"), age_sec=age)


class PipelineLock:
    """Context manager acquiring an exclusive on-disk lock.

    A lockfile left by a dead PID on this host is stale and gets reclaimed
    (with a warning). Anything else - a live PID, another host, an unreadable
    file - is respected.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False
        self.reclaimed_stale = False

    def acquire(self) -> "PipelineLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Two attempts: the second covers the case where we legitimately cleared
        # a stale lock (or it vanished) between the failed create and the read.
        for _ in range(2):
            try:
                self._create_exclusive()
                self.acquired = True
                return self
            except FileExistsError:
                self._resolve_existing()
        raise LockBusyError(
            f"could not acquire {self.path}: another process keeps recreating it. "
            f"Check for a runaway pipeline run before retrying."
        )

    def _create_exclusive(self) -> None:
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, LOCK_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    def _resolve_existing(self) -> None:
        """Decide whether the existing lock may be cleared. Raises if not."""
        existing = read_lock(self.path)
        if existing is None:
            return  # vanished under us; the retry will take it

        if not existing.parsed:
            raise LockBusyError(
                f"{self.path} exists but cannot be read. Treating it as a live run: a "
                f"half-synced lockfile is more likely an active pipeline than a dead one. "
                f"If you are certain nothing is running, delete it by hand."
            )
        if not existing.same_host:
            raise LockBusyError(
                f"pipeline lock held by pid {existing.pid} on a different machine "
                f"({existing.host}, since {existing.started_at}). This repo is Drive-synced; "
                f"wait for that run to finish rather than assuming its PID is dead."
            )
        if existing.is_live and existing.pid != os.getpid():
            raise LockBusyError(
                f"pipeline already running (pid {existing.pid} on {existing.host}, "
                f"started {existing.started_at}). Wait for it to finish; never run two "
                f"instances, and never run alongside podpub.py."
            )

        # Dead PID on this host (or our own leftover): safe to clear.
        self.reclaimed_stale = True
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            current = read_lock(self.path)
            if current is None:
                return
            if current.parsed and current.pid == os.getpid() and current.same_host:
                self.path.unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            self.acquired = False

    def __enter__(self) -> "PipelineLock":
        return self.acquire()

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.release()
