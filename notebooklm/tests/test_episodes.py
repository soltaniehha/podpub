"""Queue scanning, episode identity, title/slug derivation."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from notebooklm.episodes import (
    DEFAULT_FOCUS_TEMPLATE,
    episode_key,
    humanize,
    load_episode,
    scan_queue,
    slugify,
)
from notebooklm.tests.fakes import make_episode_dir, silent_logger


class EpisodeKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _pdf(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path

    def test_key_is_content_addressed_not_name_addressed(self) -> None:
        a = self._pdf("paper.pdf", b"%PDF alpha")
        b = self._pdf("2506.22355.pdf", b"%PDF alpha")
        self.assertEqual(episode_key([a]), episode_key([b]))

    def test_key_is_order_independent(self) -> None:
        a = self._pdf("a.pdf", b"%PDF alpha")
        b = self._pdf("b.pdf", b"%PDF beta")
        self.assertEqual(episode_key([a, b]), episode_key([b, a]))

    def test_different_content_yields_different_key(self) -> None:
        a = self._pdf("a.pdf", b"%PDF alpha")
        b = self._pdf("b.pdf", b"%PDF beta")
        self.assertNotEqual(episode_key([a]), episode_key([b]))

    def test_adding_a_paper_changes_the_key(self) -> None:
        a = self._pdf("a.pdf", b"%PDF alpha")
        b = self._pdf("b.pdf", b"%PDF beta")
        self.assertNotEqual(episode_key([a]), episode_key([a, b]))

    def test_key_requires_at_least_one_pdf(self) -> None:
        with self.assertRaises(ValueError):
            episode_key([])


class TitleAndSlugTest(unittest.TestCase):
    def test_humanize_matches_podpub_title_casing(self) -> None:
        self.assertEqual(humanize("why_physical_robots-need_AI"),
                         "Why Physical Robots Need AI")

    def test_slug_round_trips_through_humanize(self) -> None:
        """podpub re-derives the episode title from the delivered filename, and
        its clean_title capitalizes every word - so does ours."""
        title = "Embodied AI Agents: Modeling the World"
        self.assertEqual(slugify(title), "Embodied_AI_Agents_Modeling_the_World")
        self.assertEqual(humanize(slugify(title)), "Embodied AI Agents Modeling The World")

    def test_slug_has_no_characters_needing_url_escaping(self) -> None:
        """The slug becomes a filename inside the feed's enclosure URL, so
        restrict it to ASCII word characters - `str.isalnum()` is not enough,
        it happily accepts 'é' and every other non-ASCII letter."""
        slug = slugify("Robots, Physics & 'Social' Intelligence? Café — 50%")
        self.assertRegex(slug, r"\A[A-Za-z0-9_]+\Z")
        self.assertEqual(quote(slug), slug, "slug must survive URL quoting unchanged")

    def test_accents_are_folded_not_dropped(self) -> None:
        self.assertEqual(slugify("Schrödinger's Café"), "Schrodingers_Cafe")


class LoadEpisodeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.queue = Path(self.tmp.name) / "queue"
        self.queue.mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def test_defaults_derive_from_folder_name(self) -> None:
        directory = make_episode_dir(self.queue, "embodied_ai_agents")
        episode = load_episode(directory)
        assert episode is not None
        self.assertEqual(episode.title, "Embodied Ai Agents")
        self.assertEqual(episode.focus, DEFAULT_FOCUS_TEMPLATE.format(title="Embodied Ai Agents"))
        self.assertIsNone(episode.sidecar)
        self.assertFalse(episode.has_sidecar)

    def test_title_and_focus_files_win(self) -> None:
        directory = make_episode_dir(
            self.queue, "ep1", title="Could AI Pass Introductory Physics",
            focus="Focus on the grading rubric and the failure modes.",
        )
        episode = load_episode(directory)
        assert episode is not None
        self.assertEqual(episode.title, "Could AI Pass Introductory Physics")
        self.assertEqual(episode.focus, "Focus on the grading rubric and the failure modes.")
        self.assertEqual(episode.slug, "Could_AI_Pass_Introductory_Physics")

    def test_sidecar_named_after_slug_is_preferred(self) -> None:
        directory = make_episode_dir(self.queue, "ep2", title="Deep Dive")
        (directory / "Deep_Dive.md").write_text("chosen", encoding="utf-8")
        (directory / "notes.md").write_text("ignored", encoding="utf-8")
        episode = load_episode(directory)
        assert episode is not None
        self.assertEqual(episode.sidecar.name, "Deep_Dive.md")

    def test_lone_md_file_is_accepted_as_the_sidecar(self) -> None:
        directory = make_episode_dir(self.queue, "ep3", sidecar="body", sidecar_name="anything.md")
        episode = load_episode(directory)
        assert episode is not None
        self.assertEqual(episode.sidecar.name, "anything.md")

    def test_ambiguous_md_files_yield_no_sidecar(self) -> None:
        directory = make_episode_dir(self.queue, "ep4")
        (directory / "one.md").write_text("a", encoding="utf-8")
        (directory / "two.md").write_text("b", encoding="utf-8")
        episode = load_episode(directory)
        assert episode is not None
        self.assertIsNone(episode.sidecar)

    def test_multi_pdf_folder_keeps_all_papers(self) -> None:
        directory = make_episode_dir(
            self.queue, "pairing",
            pdfs={"first.pdf": b"%PDF one", "second.pdf": b"%PDF two"},
        )
        episode = load_episode(directory)
        assert episode is not None
        self.assertEqual([p.name for p in episode.pdfs], ["first.pdf", "second.pdf"])

    def test_folder_without_pdfs_is_not_an_episode_but_warns(self) -> None:
        directory = self.queue / "empty"
        directory.mkdir()
        (directory / "title.txt").write_text("no papers", encoding="utf-8")
        log = mock.Mock()
        self.assertIsNone(load_episode(directory, log))
        warning = " ".join(str(a) for call in log.warning.call_args_list for a in call[0])
        self.assertIn("no PDFs", warning)

    def test_symlinked_pdf_is_skipped(self) -> None:
        """Uploading through a symlink would send whatever it points at."""
        directory = make_episode_dir(self.queue, "linked", pdfs={"real.pdf": b"%PDF real"})
        secret = self.queue.parent / "elsewhere.pdf"
        secret.write_bytes(b"%PDF not-ours")
        (directory / "link.pdf").symlink_to(secret)

        episode = load_episode(directory, silent_logger())
        assert episode is not None
        self.assertEqual([p.name for p in episode.pdfs], ["real.pdf"])


class ScanQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.queue = Path(self.tmp.name) / "queue"
        self.queue.mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def test_missing_queue_dir_scans_empty(self) -> None:
        self.assertEqual(scan_queue(self.queue / "nope"), [])

    def test_orders_by_folder_mtime_oldest_first(self) -> None:
        older = make_episode_dir(self.queue, "second", pdfs={"b.pdf": b"%PDF b"})
        newer = make_episode_dir(self.queue, "first", pdfs={"a.pdf": b"%PDF a"})
        now = time.time()
        os.utime(older, (now - 1000, now - 1000))
        os.utime(newer, (now, now))
        self.assertEqual([e.directory.name for e in scan_queue(self.queue)], ["second", "first"])

    def test_hidden_folders_and_loose_files_are_ignored(self) -> None:
        make_episode_dir(self.queue, ".hidden")
        (self.queue / "stray.pdf").write_bytes(b"%PDF stray")
        make_episode_dir(self.queue, "real")
        found = scan_queue(self.queue, silent_logger())
        self.assertEqual([e.directory.name for e in found], ["real"])

    def test_a_loose_pdf_in_the_queue_root_is_called_out(self) -> None:
        """Silently invisible is the worst outcome: the agent believes it queued
        an episode and no error ever appears."""
        (self.queue / "stray.pdf").write_bytes(b"%PDF stray")
        log = mock.Mock()
        scan_queue(self.queue, log)
        warning = " ".join(str(a) for call in log.warning.call_args_list for a in call[0])
        self.assertIn("stray.pdf", warning)

    def test_symlinked_queue_folder_is_skipped(self) -> None:
        real = make_episode_dir(self.queue.parent / "outside", "target")
        (self.queue / "linked").symlink_to(real, target_is_directory=True)
        self.assertEqual(scan_queue(self.queue, silent_logger()), [])


if __name__ == "__main__":
    unittest.main()
