#!/usr/bin/env python3
"""Download -> verify -> hand off to podpub's inbox.

The handoff is the one step that touches a directory podpub owns, so it is
deliberately narrow: write to a temp path on the same volume, prove the file is
real audio with ffprobe, then os.replace it into place. A file that fails
verification is quarantined with a reason - never deleted, never delivered.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .scrub import scrub_text
from .verify import Verification, verify_audio

AUDIO_SUFFIX = ".m4a"
SIDECAR_SUFFIX = ".md"


class DeliveryError(RuntimeError):
    """Delivery could not proceed without risking user data."""


class InboxOccupiedError(DeliveryError):
    """The destination filename is taken. The download is untouched and valid.

    Distinct from a verification failure: nothing is wrong with the audio, so it
    stays in tmp/ and the episode stays resumable. Clear the inbox, re-run, and
    it delivers.
    """


@dataclass(frozen=True)
class Delivered:
    audio_path: Path
    sidecar_path: Path | None
    verification: Verification


def temp_target(tmp_dir: Path, slug: str) -> Path:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir / f"{slug}{AUDIO_SUFFIX}"


def quarantine(path: Path, quarantine_dir: Path, reason: str,
               log: logging.Logger | None = None) -> Path:
    """Move a suspect file aside with a sibling .reason.txt. Never deletes."""
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    target = quarantine_dir / f"{stamp}-{path.name}"
    if path.exists():
        shutil.move(str(path), str(target))
    target.with_suffix(target.suffix + ".reason.txt").write_text(
        f"quarantined: {datetime.now().astimezone().isoformat(timespec='seconds')}\n"
        f"source: {path}\n"
        f"reason: {scrub_text(reason)}\n",
        encoding="utf-8",
    )
    if log:
        log.error("quarantined %s -> %s (%s)", path.name, target, reason)
    return target


def deliver(
    downloaded: Path,
    *,
    slug: str,
    inbox_dir: Path,
    quarantine_dir: Path,
    ffprobe_bin: str = "ffprobe",
    min_duration_sec: float = 0.0,
    sidecar: Path | None = None,
    log: logging.Logger | None = None,
) -> Delivered:
    """Verify `downloaded` and move it into `inbox_dir` as `<slug>.m4a`.

    Raises DeliveryError when the file is bad (after quarantining it), or
    InboxOccupiedError when the destination is taken (leaving the download in
    place, exactly where the message says it is).
    """
    log = log or logging.getLogger("nlm.delivery")

    result = verify_audio(downloaded, ffprobe_bin, min_duration_sec=min_duration_sec)
    if not result.ok:
        quarantine(downloaded, quarantine_dir, result.reason, log)
        raise DeliveryError(f"verification failed: {result.reason}")

    inbox_dir.mkdir(parents=True, exist_ok=True)
    dest = inbox_dir / f"{slug}{AUDIO_SUFFIX}"
    if dest.exists():
        raise InboxOccupiedError(
            f"{dest} already exists - refusing to overwrite. Publish or rename the existing "
            f"file, then re-run. The downloaded audio is waiting at {downloaded}."
        )

    # Same volume (both live inside the repo), so this is atomic.
    os.replace(downloaded, dest)
    # podpub numbers episodes by mtime; stamping delivery time keeps a batch in
    # the order the pipeline processed it.
    now = time.time()
    os.utime(dest, (now, now))
    log.info("delivered audio: %s (%.1f min, %s)", dest, result.duration / 60, result.codec)

    sidecar_dest: Path | None = None
    if sidecar is not None and sidecar.exists():
        candidate = dest.with_suffix(SIDECAR_SUFFIX)
        if candidate.exists():
            # An agent may have written the description straight into the inbox.
            # Theirs is the newer intent and this pipeline deletes nothing.
            log.warning(
                "sidecar %s already exists - keeping it and NOT copying %s. "
                "Compare them by hand if that is a surprise.", candidate, sidecar,
            )
            sidecar_dest = candidate
        else:
            shutil.copy2(sidecar, candidate)
            os.utime(candidate, (now, now))
            sidecar_dest = candidate
            log.info("delivered sidecar: %s", sidecar_dest)
    else:
        log.warning(
            "NO SIDECAR for %s - podpub will fall back to a generic 'Episode N' description. "
            "Write inbox/%s.md per the format in CLAUDE.md before publishing.",
            dest.name, slug,
        )

    return Delivered(audio_path=dest, sidecar_path=sidecar_dest, verification=result)
