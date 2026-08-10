#!/usr/bin/env python3
"""Watch-folder scanning: notebooklm/queue/<episode>/ -> QueuedEpisode.

Folder convention (mirrors podpub's inbox/ drop zone):

    queue/embodied-ai-agents/
        2506.22355.pdf          one or more PDFs (required)
        title.txt               optional - episode title, first line wins
        focus.txt               optional - focus prompt for the hosts
        Embodied_AI_Agents.md   optional - podpub description sidecar

The episode identity is the hash of the PDF *contents*, so renaming the folder
or reordering files never looks like a new episode - which is what keeps a
re-run from spending another audio-generation quota unit.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

PDF_SUFFIX = ".pdf"
TITLE_FILE = "title.txt"
FOCUS_FILE = "focus.txt"
_HASH_CHUNK = 1 << 20

# Default prompt when focus.txt is absent. Deliberately generic: a real focus
# prompt derived from the paper's thesis produces a much better deep dive, so
# INSTRUCTIONS.md tells the agent to write one.
DEFAULT_FOCUS_TEMPLATE = (
    "Deep dive into {title}. Explain the central argument in plain language, "
    "walk through the named technical contributions, and close on the "
    "implications and open questions."
)


@dataclass(frozen=True)
class QueuedEpisode:
    """One ready-to-generate episode folder."""

    key: str
    slug: str
    title: str
    focus: str
    directory: Path
    pdfs: tuple[Path, ...]
    sidecar: Path | None

    @property
    def has_sidecar(self) -> bool:
        return self.sidecar is not None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def episode_key(pdfs: list[Path] | tuple[Path, ...]) -> str:
    """Stable id for a set of PDFs: hash of their sorted content hashes.

    Order-independent and name-independent, so `paper.pdf` renamed to
    `2506.22355.pdf` is still the same episode.
    """
    if not pdfs:
        raise ValueError("episode_key requires at least one PDF")
    digests = sorted(file_sha256(p) for p in pdfs)
    return hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()[:32]


def humanize(name: str) -> str:
    """Folder/slug -> display title. Mirrors podpub.clean_title so the title
    survives the round trip through the delivered filename."""
    text = re.sub(r"[_\-]+", " ", name)
    text = re.sub(r"\s+", " ", text).strip()

    def cap(word: str) -> str:
        if word.isupper() and 2 <= len(word) <= 5 and word.isalpha():
            return word
        return word[:1].upper() + word[1:] if word else word

    return " ".join(cap(w) for w in text.split())


def slugify(title: str) -> str:
    """Title -> the basename podpub will see.

    ASCII word characters and underscores only. Accents are folded rather than
    dropped ("Schrodinger", not "Schrdinger"), because the result becomes both a
    filename on a Drive-synced volume and a path segment in the feed's
    enclosure URL.
    """
    folded = unicodedata.normalize("NFKD", title)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    cleaned = re.sub(r"[^A-Za-z0-9\s\-_]", "", folded)
    cleaned = re.sub(r"[\s\-_]+", "_", cleaned).strip("_")
    return cleaned or "Episode"


def _read_first_line(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _find_sidecar(directory: Path, slug: str) -> Path | None:
    """`<slug>.md` wins; otherwise a lone .md in the folder is accepted."""
    preferred = directory / f"{slug}.md"
    if preferred.exists():
        return preferred
    candidates = sorted(p for p in directory.glob("*.md") if p.is_file())
    return candidates[0] if len(candidates) == 1 else None


def load_episode(directory: Path, log: logging.Logger | None = None) -> QueuedEpisode | None:
    """Build a QueuedEpisode from a folder, or None if it holds no usable PDFs."""
    log = log or logging.getLogger("nlm.episodes")

    pdfs: list[Path] = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name):
        if not entry.is_file() or entry.suffix.lower() != PDF_SUFFIX:
            continue
        if entry.is_symlink():
            # Uploading through a symlink would send whatever it points at.
            # notebooklm-py rejects them too, by default.
            log.warning("skipping symlinked PDF %s (symlinks are not uploaded)", entry)
            continue
        pdfs.append(entry)

    if not pdfs:
        log.warning(
            "queue folder %s has no PDFs - nothing to generate from. Add the paper(s), "
            "or remove the folder.", directory,
        )
        return None
    pdfs_t = tuple(pdfs)

    title_path = directory / TITLE_FILE
    title = _read_first_line(title_path) if title_path.exists() else ""
    if not title:
        title = humanize(directory.name)

    focus_path = directory / FOCUS_FILE
    focus = focus_path.read_text(encoding="utf-8").strip() if focus_path.exists() else ""
    if not focus:
        focus = DEFAULT_FOCUS_TEMPLATE.format(title=title)

    slug = slugify(title)
    return QueuedEpisode(
        key=episode_key(pdfs_t),
        slug=slug,
        title=title,
        focus=focus,
        directory=directory,
        pdfs=pdfs_t,
        sidecar=_find_sidecar(directory, slug),
    )


def scan_queue(queue_dir: Path, log: logging.Logger | None = None) -> list[QueuedEpisode]:
    """All episode folders, oldest folder first (delivery order = episode order)."""
    log = log or logging.getLogger("nlm.episodes")
    if not queue_dir.is_dir():
        return []
    episodes: list[QueuedEpisode] = []
    for entry in sorted(queue_dir.iterdir(), key=lambda p: (p.stat().st_mtime, p.name)):
        if entry.name.startswith("."):
            continue
        if entry.is_symlink():
            log.warning("skipping symlinked queue entry %s (symlinks are not followed)", entry)
            continue
        if not entry.is_dir():
            if entry.suffix.lower() == PDF_SUFFIX:
                log.warning(
                    "%s is loose in the queue root and will be ignored - each episode needs "
                    "its own subfolder.", entry.name,
                )
            continue
        episode = load_episode(entry, log)
        if episode is not None:
            episodes.append(episode)
    return episodes
