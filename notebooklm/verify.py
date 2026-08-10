#!/usr/bin/env python3
"""ffprobe verification for downloaded audio.

A NotebookLM download that silently returns an HTML error page, a zero-byte
file, or an unexpected codec must never reach podpub's inbox - podpub would
publish it to a live feed. Anything that fails here gets quarantined.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

FFPROBE_ARGS = ["-v", "error", "-print_format", "json", "-show_format", "-show_streams"]

EXPECTED_CODEC = "aac"
# ffprobe reports MP4/M4A containers with a comma-joined brand list.
EXPECTED_CONTAINER_TOKENS = frozenset({"mp4", "m4a", "mov", "3gp", "isom", "ipod"})

FFPROBE_MISSING_HINT = "ffprobe not found. Install it with: brew install ffmpeg"


class FfprobeMissingError(RuntimeError):
    def __init__(self, binary: str) -> None:
        super().__init__(f"{binary!r} is not on PATH. {FFPROBE_MISSING_HINT}")


@dataclass(frozen=True)
class Verification:
    ok: bool
    reason: str
    codec: str | None = None
    container: str | None = None
    duration: float = 0.0
    size_bytes: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "codec": self.codec,
            "container": self.container,
            "duration_sec": round(self.duration, 2),
            "bytes": self.size_bytes,
        }


def probe(path: Path, ffprobe_bin: str = "ffprobe", *, timeout: int = 60) -> dict:
    try:
        proc = subprocess.run(
            [ffprobe_bin, *FFPROBE_ARGS, str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FfprobeMissingError(ffprobe_bin) from exc
    if proc.returncode != 0:
        raise ValueError(f"ffprobe failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ffprobe returned non-JSON output: {exc}") from exc


def verify_audio(path: Path, ffprobe_bin: str = "ffprobe", *,
                 min_duration_sec: float = 0.0) -> Verification:
    """Expect AAC audio in an MP4/M4A container with a nonzero duration.

    `min_duration_sec` guards against a technically-valid but far-too-short
    file (a truncated or failed generation). It defaults to 0 so the codec and
    container checks can be exercised on short fixtures; the pipeline passes
    `config.min_duration_sec` (default 300).
    """
    if not path.exists():
        return Verification(False, f"file does not exist: {path}")
    size = path.stat().st_size
    if size == 0:
        return Verification(False, "file is empty (0 bytes)", size_bytes=0)

    try:
        data = probe(path, ffprobe_bin)
    except FfprobeMissingError:
        raise
    except ValueError as exc:
        return Verification(False, str(exc), size_bytes=size)

    fmt = data.get("format") or {}
    container = str(fmt.get("format_name") or "")
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    audio_streams = [s for s in (data.get("streams") or []) if s.get("codec_type") == "audio"]
    if not audio_streams:
        return Verification(False, "no audio stream found", container=container,
                            duration=duration, size_bytes=size)
    codec = str(audio_streams[0].get("codec_name") or "")

    if codec.lower() != EXPECTED_CODEC:
        return Verification(False, f"unexpected codec {codec!r} (expected {EXPECTED_CODEC})",
                            codec=codec, container=container, duration=duration, size_bytes=size)

    tokens = {t.strip().lower() for t in container.split(",") if t.strip()}
    if not tokens & EXPECTED_CONTAINER_TOKENS:
        return Verification(False, f"unexpected container {container!r} (expected MP4/M4A)",
                            codec=codec, container=container, duration=duration, size_bytes=size)

    if duration <= 0:
        return Verification(False, "zero-length audio", codec=codec, container=container,
                            duration=duration, size_bytes=size)

    if min_duration_sec and duration < min_duration_sec:
        return Verification(
            False,
            f"audio is only {duration:.0f}s, below the {min_duration_sec:.0f}s minimum for a "
            f"Deep Dive (likely a truncated or failed generation)",
            codec=codec, container=container, duration=duration, size_bytes=size,
        )

    return Verification(True, "ok", codec=codec, container=container,
                        duration=duration, size_bytes=size)
