"""Verification against the real ffprobe.

test_verify.py mocks `probe`, so it proves the routing logic but never that our
expectations match what ffprobe actually prints for a NotebookLM-shaped file.
These tests use real ffmpeg/ffprobe and skip when either is absent, so a
machine without them still runs the rest of the suite.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from notebooklm.verify import EXPECTED_CODEC, verify_audio

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def synth_m4a(path: Path, seconds: float) -> None:
    subprocess.run(
        [FFMPEG, "-nostdin", "-y", "-f", "lavfi", "-i",
         f"sine=frequency=440:duration={seconds}", "-c:a", "aac", "-b:a", "32k", str(path)],
        check=True, capture_output=True,
    )


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe not installed")
class RealFfprobeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_real_aac_m4a_passes_and_reports_its_duration(self) -> None:
        path = self.root / "episode.m4a"
        synth_m4a(path, 3)
        result = verify_audio(path, FFPROBE)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.codec, EXPECTED_CODEC)
        self.assertAlmostEqual(result.duration, 3.0, delta=0.3)
        self.assertGreater(result.size_bytes, 0)

    def test_container_token_matching_survives_ffprobes_real_brand_list(self) -> None:
        """ffprobe reports 'mov,mp4,m4a,3gp,3g2,mj2' - the token set must match it."""
        path = self.root / "episode.m4a"
        synth_m4a(path, 1)
        self.assertIn("m4a", verify_audio(path, FFPROBE).container or "")

    def test_html_error_page_named_m4a_is_rejected(self) -> None:
        path = self.root / "not-audio.m4a"
        path.write_text("<html><body>Sign in to continue</body></html>", encoding="utf-8")
        result = verify_audio(path, FFPROBE)
        self.assertFalse(result.ok)
        self.assertIn("ffprobe failed", result.reason)

    def test_truncated_download_is_rejected(self) -> None:
        path = self.root / "truncated.m4a"
        synth_m4a(path, 3)
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 3])
        self.assertFalse(verify_audio(path, FFPROBE).ok)

    def test_mp3_audio_is_rejected_even_though_it_is_real_audio(self) -> None:
        path = self.root / "episode.mp3"
        subprocess.run(
            [FFMPEG, "-nostdin", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-c:a", "libmp3lame", str(path)],
            check=True, capture_output=True,
        )
        result = verify_audio(path, FFPROBE)
        self.assertFalse(result.ok)
        self.assertIn("codec", result.reason)


if __name__ == "__main__":
    unittest.main()
