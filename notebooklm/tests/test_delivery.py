"""Delivery into podpub's inbox, sidecar handling, and quarantine routing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notebooklm import delivery
from notebooklm.delivery import DeliveryError, deliver, quarantine, temp_target
from notebooklm.tests.fakes import silent_logger
from notebooklm.verify import Verification

PASS = Verification(True, "ok", codec="aac", container="mov,mp4,m4a", duration=1200.0, size_bytes=99)
FAIL = Verification(False, "unexpected codec 'mp3' (expected aac)", codec="mp3")


class DeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inbox = self.root / "inbox"
        self.quarantine_dir = self.root / "quarantine"
        self.downloaded = temp_target(self.root / "tmp", "Deep_Dive")
        self.downloaded.write_bytes(b"audio-bytes")
        self.log = silent_logger()
        self.addCleanup(self.tmp.cleanup)

    def _deliver(self, sidecar: Path | None = None, verification: Verification = PASS):
        with mock.patch.object(delivery, "verify_audio", return_value=verification):
            return deliver(
                self.downloaded,
                slug="Deep_Dive",
                inbox_dir=self.inbox,
                quarantine_dir=self.quarantine_dir,
                sidecar=sidecar,
                log=self.log,
            )

    def test_verified_audio_lands_in_inbox_under_the_slug(self) -> None:
        result = self._deliver()
        self.assertEqual(result.audio_path, self.inbox / "Deep_Dive.m4a")
        self.assertTrue(result.audio_path.exists())
        self.assertFalse(self.downloaded.exists(), "temp file should have been moved, not copied")

    def test_sidecar_is_copied_next_to_the_audio(self) -> None:
        sidecar = self.root / "notes.md"
        sidecar.write_text("Paper Title (June 2025)\n\nIn this episode we unpack...", encoding="utf-8")
        result = self._deliver(sidecar=sidecar)
        self.assertEqual(result.sidecar_path, self.inbox / "Deep_Dive.md")
        self.assertIn("we unpack", result.sidecar_path.read_text(encoding="utf-8"))
        self.assertTrue(sidecar.exists(), "source sidecar must remain in the queue folder")

    def test_missing_sidecar_still_delivers_the_audio(self) -> None:
        result = self._deliver(sidecar=None)
        self.assertTrue(result.audio_path.exists())
        self.assertIsNone(result.sidecar_path)

    def test_failed_verification_quarantines_instead_of_delivering(self) -> None:
        with self.assertRaises(DeliveryError):
            self._deliver(verification=FAIL)
        self.assertFalse((self.inbox / "Deep_Dive.m4a").exists())
        quarantined = list(self.quarantine_dir.glob("*Deep_Dive.m4a"))
        self.assertEqual(len(quarantined), 1)
        reason = quarantined[0].with_suffix(quarantined[0].suffix + ".reason.txt")
        self.assertIn("unexpected codec", reason.read_text(encoding="utf-8"))

    def test_existing_inbox_file_is_never_overwritten(self) -> None:
        self.inbox.mkdir(parents=True)
        (self.inbox / "Deep_Dive.m4a").write_bytes(b"the-real-episode")
        with self.assertRaises(DeliveryError) as ctx:
            self._deliver()
        self.assertIn("refusing to overwrite", str(ctx.exception))
        self.assertEqual((self.inbox / "Deep_Dive.m4a").read_bytes(), b"the-real-episode")
        self.assertTrue(self.downloaded.exists(), "download must be preserved for recovery")

    def test_quarantine_never_deletes_and_records_a_reason(self) -> None:
        target = quarantine(self.downloaded, self.quarantine_dir, "because", self.log)
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"audio-bytes")
        self.assertIn("because",
                      target.with_suffix(target.suffix + ".reason.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
