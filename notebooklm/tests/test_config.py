"""config.yaml loading: overrides, path resolution, and the fallback defaults.

The fallback matters more than it looks: any key absent from config.yaml keeps
the packaged default, which points at the *real* notebooklm/ working
directories. A config written for a test or a second machine that omits
`state_file` silently shares production state.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from notebooklm.config import DEFAULT_DAILY_CAP, Config, load_config

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ModuleNotFoundError:  # pragma: no cover - the repo venv has PyYAML
    HAVE_YAML = False


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class LoadConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = self.root / "pkg"
        self.base.mkdir()
        self.path = self.root / "config.yaml"
        self.addCleanup(self.tmp.cleanup)

    def write(self, text: str) -> Path:
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def test_missing_file_names_the_copy_command(self) -> None:
        with self.assertRaises(FileNotFoundError) as ctx:
            load_config(self.root / "nope.yaml", base=self.base)
        self.assertIn("cp ", str(ctx.exception))

    def test_non_mapping_yaml_is_rejected(self) -> None:
        self.write("- just\n- a\n- list\n")
        with self.assertRaises(ValueError):
            load_config(self.path, base=self.base)

    def test_empty_file_falls_back_to_defaults(self) -> None:
        self.write("")
        cfg = load_config(self.path, base=self.base)
        self.assertEqual(cfg.queue_dir, self.base / "queue")
        self.assertEqual(cfg.daily_audio_cap, DEFAULT_DAILY_CAP)

    def test_relative_paths_resolve_against_the_package_dir_not_the_config_dir(self) -> None:
        """A config living elsewhere still resolves relatives against the package."""
        self.write("queue_dir: my-queue\n")
        cfg = load_config(self.path, base=self.base)
        self.assertEqual(cfg.queue_dir, self.base / "my-queue")

    def test_absolute_paths_are_kept_verbatim(self) -> None:
        elsewhere = self.root / "elsewhere"
        self.write(f"state_file: {elsewhere}/state.json\n")
        cfg = load_config(self.path, base=self.base)
        self.assertEqual(cfg.state_file, elsewhere / "state.json")

    def test_absent_keys_keep_the_packaged_defaults(self) -> None:
        self.write("queue_dir: /tmp/only-this-is-overridden\n")
        cfg = load_config(self.path, base=self.base)
        self.assertEqual(cfg.state_file, self.base / "state.json")
        self.assertEqual(cfg.lock_file, self.base / ".lock")

    def test_generate_block_is_merged_not_replaced(self) -> None:
        self.write("generate:\n  length: short\n")
        cfg = load_config(self.path, base=self.base)
        self.assertEqual(cfg.generate.length, "short")
        self.assertEqual(cfg.generate.format, "deep-dive")
        self.assertEqual(cfg.generate.timeout, 1800)

    def test_numeric_settings_are_coerced_to_int(self) -> None:
        self.write("daily_audio_cap: '15'\ngenerate:\n  timeout: '2400'\n")
        cfg = load_config(self.path, base=self.base)
        self.assertEqual(cfg.daily_audio_cap, 15)
        self.assertEqual(cfg.generate.timeout, 2400)

    def test_zero_cap_is_honored_not_treated_as_unset(self) -> None:
        self.write("daily_audio_cap: 0\n")
        self.assertEqual(load_config(self.path, base=self.base).daily_audio_cap, 0)

    def test_null_profile_stays_none(self) -> None:
        self.write("profile: null\n")
        self.assertIsNone(load_config(self.path, base=self.base).profile)

    def test_tilde_in_paths_is_expanded(self) -> None:
        self.write("inbox_dir: ~/podpub-inbox\n")
        cfg = load_config(self.path, base=self.base)
        self.assertEqual(cfg.inbox_dir, Path.home() / "podpub-inbox")


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class ConfigBaseDefaultTest(unittest.TestCase):
    """Without an explicit `base`, everything resolves next to the config file.

    A scratch config that only sets queue_dir/inbox_dir used to inherit the
    packaged defaults, so a staging run shared production's state.json and
    lockfile - and could regenerate or double-bill real episodes.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_absent_keys_land_beside_the_config_not_in_the_package(self) -> None:
        from notebooklm.config import PKG_DIR

        scratch = self.root / "staging"
        scratch.mkdir()
        path = scratch / "config.yaml"
        path.write_text("queue_dir: q\ninbox_dir: inbox\n", encoding="utf-8")

        cfg = load_config(path)
        self.assertEqual(cfg.state_file, scratch / "state.json")
        self.assertEqual(cfg.lock_file, scratch / ".lock")
        self.assertEqual(cfg.queue_dir, scratch / "q")
        self.assertNotEqual(cfg.state_file, PKG_DIR / "state.json")
        self.assertNotEqual(cfg.lock_file, PKG_DIR / ".lock")

    def test_the_packaged_config_location_is_unchanged(self) -> None:
        """notebooklm/config.yaml still resolves against notebooklm/."""
        from notebooklm.config import PKG_DIR

        path = self.root / "config.yaml"
        path.write_text("state_file: state.json\n", encoding="utf-8")
        cfg = load_config(path)
        self.assertEqual(cfg.state_file, self.root / "state.json")
        # Same rule applied to the real location yields the packaged paths.
        self.assertEqual(Config.defaults(PKG_DIR).state_file, PKG_DIR / "state.json")


class EnsureDirsTest(unittest.TestCase):
    def test_ensure_dirs_creates_workdirs_but_not_the_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.defaults(Path(tmp) / "pkg")
            cfg.ensure_dirs()
            self.assertTrue(cfg.queue_dir.is_dir())
            self.assertTrue(cfg.tmp_dir.is_dir())
            self.assertTrue(cfg.quarantine_dir.is_dir())
            self.assertTrue(cfg.log_file.parent.is_dir())
            self.assertFalse(cfg.inbox_dir.exists(), "podpub owns inbox/; delivery creates it")


if __name__ == "__main__":
    unittest.main()
