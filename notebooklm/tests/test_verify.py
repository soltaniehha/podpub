"""ffprobe verification: what passes, what gets rejected, missing binary."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notebooklm import verify
from notebooklm.verify import FfprobeMissingError, verify_audio


def probe_payload(codec: str = "aac", container: str = "mov,mp4,m4a,3gp,3g2,mj2",
                  duration: str = "1234.5") -> dict:
    return {
        "streams": [{"codec_type": "audio", "codec_name": codec}],
        "format": {"format_name": container, "duration": duration},
    }


class VerifyAudioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "episode.m4a"
        self.path.write_bytes(b"x" * 4096)
        self.addCleanup(self.tmp.cleanup)

    def _verify_with(self, payload: dict) -> verify.Verification:
        with mock.patch.object(verify, "probe", return_value=payload):
            return verify_audio(self.path)

    def test_aac_in_mp4_passes(self) -> None:
        result = self._verify_with(probe_payload())
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.codec, "aac")
        self.assertEqual(result.duration, 1234.5)
        self.assertEqual(result.size_bytes, 4096)

    def test_wrong_codec_is_rejected(self) -> None:
        result = self._verify_with(probe_payload(codec="mp3"))
        self.assertFalse(result.ok)
        self.assertIn("codec", result.reason)

    def test_wrong_container_is_rejected(self) -> None:
        result = self._verify_with(probe_payload(container="matroska,webm"))
        self.assertFalse(result.ok)
        self.assertIn("container", result.reason)

    def test_zero_duration_is_rejected(self) -> None:
        result = self._verify_with(probe_payload(duration="0"))
        self.assertFalse(result.ok)
        self.assertIn("zero-length", result.reason)

    def test_no_audio_stream_is_rejected(self) -> None:
        payload = {"streams": [{"codec_type": "video", "codec_name": "h264"}],
                   "format": {"format_name": "mov,mp4", "duration": "10"}}
        result = self._verify_with(payload)
        self.assertFalse(result.ok)
        self.assertIn("no audio stream", result.reason)

    def test_empty_file_is_rejected_without_probing(self) -> None:
        empty = Path(self.tmp.name) / "empty.m4a"
        empty.touch()
        with mock.patch.object(verify, "probe", side_effect=AssertionError("must not probe")):
            result = verify_audio(empty)
        self.assertFalse(result.ok)
        self.assertIn("empty", result.reason)

    def test_missing_file_is_rejected(self) -> None:
        result = verify_audio(Path(self.tmp.name) / "nope.m4a")
        self.assertFalse(result.ok)
        self.assertIn("does not exist", result.reason)

    def test_html_error_page_masquerading_as_audio_is_rejected(self) -> None:
        """ffprobe refuses to parse it -> ValueError -> failed verification, not a crash."""
        with mock.patch.object(verify, "probe", side_effect=ValueError("ffprobe failed (1)")):
            result = verify_audio(self.path)
        self.assertFalse(result.ok)
        self.assertIn("ffprobe failed", result.reason)

    def test_missing_ffprobe_raises_with_brew_hint(self) -> None:
        with self.assertRaises(FfprobeMissingError) as ctx:
            verify_audio(self.path, ffprobe_bin="definitely-not-ffprobe-xyz")
        self.assertIn("brew install ffmpeg", str(ctx.exception))

    def test_as_dict_is_json_serializable_summary(self) -> None:
        result = self._verify_with(probe_payload())
        self.assertEqual(
            set(result.as_dict()), {"codec", "container", "duration_sec", "bytes"}
        )


if __name__ == "__main__":
    unittest.main()
