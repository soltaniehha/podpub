#!/usr/bin/env python3
"""Config loading for the NotebookLM pipeline.

config.yaml is gitignored; config.yaml.example is the tracked template.

Paths may be absolute, or relative to *the directory holding the config file*.
That last part matters: resolving relatives (and defaults for absent keys)
against the package directory meant a scratch config elsewhere silently shared
production's state.json and lockfile - a staging run could regenerate or
double-bill real episodes. A config in notebooklm/ still behaves identically,
since that directory is both its parent and the package.

PyYAML is imported lazily so the pure-logic modules (and their tests) stay
dependency free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PKG_DIR = Path(__file__).resolve().parent
REPO_DIR = PKG_DIR.parent
CONFIG_FILE = PKG_DIR / "config.yaml"
EXAMPLE_FILE = PKG_DIR / "config.yaml.example"

# NotebookLM's own daily audio ceiling: 3 on free, 20 on AI Pro. We default to
# the free tier; 15 is the documented safe setting for AI Pro (headroom for
# manual generations in the web UI on the same account).
DEFAULT_DAILY_CAP = 3


@dataclass
class GenerateSettings:
    """Knobs for `notebooklm generate audio`."""

    format: str = "deep-dive"
    length: str = "long"
    # Long deep dives run 10-20 minutes. Anything under 1200s reports a false
    # failure while the job keeps running server-side.
    timeout: int = 1800
    interval: int = 10
    retry: int = 3
    # NotebookLM refuses audio generation while uploaded sources are still
    # ingesting. Observed on the first real run: 3 PDFs (34MB) took ~90s to all
    # report `ready`. We wait up to this long before giving up on an episode.
    source_ready_timeout: int = 300


@dataclass
class Config:
    queue_dir: Path
    inbox_dir: Path
    state_file: Path
    log_file: Path
    tmp_dir: Path
    quarantine_dir: Path
    lock_file: Path
    notebooklm_bin: str = "notebooklm"
    ffprobe_bin: str = "ffprobe"
    profile: str | None = None
    daily_audio_cap: int = DEFAULT_DAILY_CAP
    command_timeout: int = 120
    # A real long Deep Dive runs 15-45 minutes. Anything materially shorter is a
    # truncated or failed generation, not an episode - quarantine it rather than
    # letting podpub publish it to a live feed.
    min_duration_sec: int = 300
    generate: GenerateSettings = field(default_factory=GenerateSettings)

    @classmethod
    def defaults(cls, base: Path = PKG_DIR) -> "Config":
        return cls(
            queue_dir=base / "queue",
            inbox_dir=base.parent / "inbox",
            state_file=base / "state.json",
            log_file=base / "logs" / "pipeline.log",
            tmp_dir=base / "tmp",
            quarantine_dir=base / "quarantine",
            lock_file=base / ".lock",
        )

    def ensure_dirs(self) -> None:
        for path in (self.queue_dir, self.tmp_dir, self.quarantine_dir, self.log_file.parent):
            path.mkdir(parents=True, exist_ok=True)


def _resolve(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (base / path)


def load_config(path: Path = CONFIG_FILE, *, base: Path | None = None) -> Config:
    """Read config.yaml, falling back to defaults for any absent key.

    Relative paths and the defaults for absent keys resolve against `base`,
    which defaults to the config file's own directory (see module docstring).
    """
    path = Path(path).expanduser()
    # absolute(), not resolve(): this repo lives under a Drive-synced path that
    # is itself a symlink, and rewriting every configured path to the
    # CloudStorage target would be confusing rather than helpful.
    base = base if base is not None else path.absolute().parent
    cfg = Config.defaults(base)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy the template and edit it:\n"
            f"  cp {EXAMPLE_FILE} {path}"
        )

    try:
        import yaml  # noqa: PLC0415 - optional at import time, required here
    except ModuleNotFoundError as exc:  # pragma: no cover - environment issue
        raise RuntimeError(
            "PyYAML is required to read config.yaml. Run the pipeline with the "
            "repo venv: .venv/bin/python notebooklm/nlm_pipeline.py"
        ) from exc

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")

    for key in ("queue_dir", "inbox_dir", "state_file", "log_file", "tmp_dir",
                "quarantine_dir", "lock_file"):
        if raw.get(key):
            setattr(cfg, key, _resolve(raw[key], base))

    for key in ("notebooklm_bin", "ffprobe_bin", "profile"):
        if raw.get(key):
            setattr(cfg, key, str(raw[key]))

    for key in ("daily_audio_cap", "command_timeout", "min_duration_sec"):
        if raw.get(key) is not None:
            setattr(cfg, key, int(raw[key]))

    gen = raw.get("generate") or {}
    if isinstance(gen, dict):
        for key in ("format", "length"):
            if gen.get(key):
                setattr(cfg.generate, key, str(gen[key]))
        for key in ("timeout", "interval", "retry", "source_ready_timeout"):
            if gen.get(key) is not None:
                setattr(cfg.generate, key, int(gen[key]))

    return cfg
