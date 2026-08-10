#!/usr/bin/env python3
"""Idempotent pipeline state: episode records + the local quota ledger.

state.json is the pipeline's memory of what has already been paid for. Losing
or corrupting it must never cause a regenerate (each one costs a quota unit and
NotebookLM exposes no API to ask how many are left), so a file that exists but
does not parse is a hard error, not a reason to start fresh.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# Episode lifecycle. Forward-only except for the two terminal error states,
# which a human/agent clears by editing state.json or re-queueing.
STATUS_NEW = "new"
STATUS_NOTEBOOK_CREATED = "notebook_created"
STATUS_SOURCES_ADDED = "sources_added"
STATUS_GENERATING = "generating"
STATUS_GENERATED = "generated"
STATUS_DELIVERED = "delivered"
STATUS_QUARANTINED = "quarantined"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = frozenset({STATUS_DELIVERED, STATUS_QUARANTINED, STATUS_FAILED})

SIDECAR_PRESENT = "present"
SIDECAR_MISSING = "missing"


class StateError(RuntimeError):
    """Base class for state problems."""


class StateCorruptError(StateError):
    """state.json exists but cannot be trusted - refuse to run."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now().astimezone().date().isoformat()


@dataclass
class EpisodeRecord:
    """One queued episode, keyed by the hash of its PDF contents."""

    key: str
    slug: str
    title: str
    queue_dir: str = ""
    status: str = STATUS_NEW
    notebook_id: str | None = None
    task_id: str | None = None
    sources_added: list[str] = field(default_factory=list)
    artifact: dict[str, Any] = field(default_factory=dict)
    output_path: str | None = None
    sidecar: str = SIDECAR_MISSING
    attempts: int = 0
    last_error: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def touch(self) -> None:
        self.updated_at = _now()

    @property
    def is_delivered(self) -> bool:
        return self.status == STATUS_DELIVERED

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EpisodeRecord":
        known = {f for f in cls.__dataclass_fields__}  # noqa: SIM118
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class QuotaLedger:
    """Local daily counter. NotebookLM offers no way to query the real one."""

    date: str = field(default_factory=today_str)
    used: int = 0

    def rollover(self, today: str | None = None) -> None:
        today = today or today_str()
        if self.date != today:
            self.date = today
            self.used = 0

    def remaining(self, cap: int, today: str | None = None) -> int:
        self.rollover(today)
        return max(0, cap - self.used)

    def consume(self, cap: int, today: str | None = None) -> None:
        if self.remaining(cap, today) <= 0:
            raise StateError(f"daily audio cap reached ({self.used}/{cap} on {self.date})")
        self.used += 1


class StateStore:
    """Load/mutate/save state.json atomically."""

    def __init__(self, path: Path, episodes: dict[str, EpisodeRecord] | None = None,
                 quota: QuotaLedger | None = None) -> None:
        self.path = path
        self.episodes: dict[str, EpisodeRecord] = episodes or {}
        self.quota = quota or QuotaLedger()

    # ----- persistence -----

    @classmethod
    def load(cls, path: Path) -> "StateStore":
        if not path.exists():
            return cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StateCorruptError(
                f"{path} is not valid JSON ({exc}). Refusing to run: a blank state would "
                f"regenerate episodes that were already paid for. Inspect the file, fix it, "
                f"or move it aside ONLY after confirming which episodes already exist in "
                f"NotebookLM (`notebooklm list`)."
            ) from exc
        if not isinstance(raw, dict):
            raise StateCorruptError(f"{path} must contain a JSON object, got {type(raw).__name__}")

        version = raw.get("version")
        if version is not None and int(version) > SCHEMA_VERSION:
            raise StateCorruptError(
                f"{path} was written by a newer schema (v{version} > v{SCHEMA_VERSION}). Upgrade "
                f"the pipeline rather than downgrading the state file."
            )

        episodes_raw = raw.get("episodes") or {}
        if not isinstance(episodes_raw, dict):
            raise StateCorruptError(f"{path}: 'episodes' must be an object")
        episodes: dict[str, EpisodeRecord] = {}
        for key, value in episodes_raw.items():
            if not isinstance(value, dict):
                raise StateCorruptError(f"{path}: episode {key!r} is not an object")
            episodes[key] = EpisodeRecord.from_dict({"key": key, **value})

        quota_raw = raw.get("quota") or {}
        quota = QuotaLedger(
            date=str(quota_raw.get("date") or today_str()),
            used=int(quota_raw.get("used") or 0),
        )
        return cls(path, episodes, quota)

    def save(self) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": _now(),
            "quota": asdict(self.quota),
            "episodes": {k: _record_payload(v) for k, v in sorted(self.episodes.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # with_name, not with_suffix: with_suffix mangles names that already
        # carry a dotted suffix (".lock" -> ".lock.lock.tmp"). Per-PID so two
        # processes can never share a staging file.
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    # ----- episodes -----

    def get(self, key: str) -> EpisodeRecord | None:
        return self.episodes.get(key)

    def upsert(self, key: str, *, slug: str, title: str, queue_dir: str) -> EpisodeRecord:
        record = self.episodes.get(key)
        if record is None:
            record = EpisodeRecord(key=key, slug=slug, title=title, queue_dir=queue_dir)
            self.episodes[key] = record
            return record
        # Folder may have been renamed/moved; the content hash is the identity.
        record.slug = slug
        record.title = title
        record.queue_dir = queue_dir
        return record


def _record_payload(record: EpisodeRecord) -> dict[str, Any]:
    data = asdict(record)
    data.pop("key", None)  # the dict key already carries it
    return data
