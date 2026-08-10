"""The subprocess wrapper: argv construction, JSON parsing, error mapping.

No real `notebooklm` process is ever spawned here - subprocess.run is patched.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from notebooklm import nlm_cli
from notebooklm.nlm_cli import (
    CODE_RATE_LIMITED,
    CODE_TIMEOUT,
    NotebookLMCLI,
    NotebookLMError,
    NotebookLMNotInstalled,
    _parse_json,
    version_matches,
)


class CompletedProcess:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class ParseJsonTest(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(_parse_json('{"a": 1}'), {"a": 1})

    def test_json_after_prose_lines(self) -> None:
        self.assertEqual(_parse_json('Working...\n{"a": 1}'), {"a": 1})

    def test_json_array(self) -> None:
        self.assertEqual(_parse_json("[1, 2]"), [1, 2])

    def test_empty_and_garbage(self) -> None:
        self.assertIsNone(_parse_json(""))
        self.assertIsNone(_parse_json("no json here"))


class WrapperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = NotebookLMCLI(binary="notebooklm")

    def _patch(self, result):
        return mock.patch.object(nlm_cli.subprocess, "run", return_value=result)

    def test_create_notebook_returns_id_and_builds_argv(self) -> None:
        payload = json.dumps({"notebook": {"id": "nb_abc", "title": "T"}})
        with self._patch(CompletedProcess(stdout=payload)) as run:
            self.assertEqual(self.cli.create_notebook("My Episode"), "nb_abc")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["notebooklm", "create"])
        self.assertIn("My Episode", argv)
        self.assertIn("--json", argv)
        self.assertFalse(run.call_args[1].get("shell", False))

    def test_profile_is_threaded_through(self) -> None:
        cli = NotebookLMCLI(binary="notebooklm", profile="podcast")
        with self._patch(CompletedProcess(stdout='{"notebook": {"id": "x"}}')) as run:
            cli.create_notebook("T")
        self.assertEqual(run.call_args[0][0][:3], ["notebooklm", "-p", "podcast"])

    def test_start_audio_never_blocks_and_returns_task_id(self) -> None:
        payload = json.dumps({"task_id": "task_9", "status": "pending"})
        with self._patch(CompletedProcess(stdout=payload)) as run:
            self.assertEqual(self.cli.start_audio("nb_1", "focus here"), "task_9")
        argv = run.call_args[0][0]
        self.assertIn("--no-wait", argv)
        self.assertIn("deep-dive", argv)
        self.assertIn("long", argv)

    def test_poll_normalizes_status(self) -> None:
        payload = json.dumps({"task_id": "t1", "status": "completed", "url": "https://x/y.m4a"})
        with self._patch(CompletedProcess(stdout=payload)):
            status = self.cli.poll("nb_1", "t1")
        self.assertTrue(status.is_complete)
        self.assertEqual(status.url, "https://x/y.m4a")

    def test_wait_timeout_reports_pending_not_failure(self) -> None:
        """A local timeout must never look like a failed generation."""
        with mock.patch.object(
            nlm_cli.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="notebooklm", timeout=5),
        ):
            status = self.cli.wait("nb_1", "t1", timeout=5)
        self.assertFalse(status.is_failed)
        self.assertEqual(status.status, "pending")

    def test_rate_limit_envelope_becomes_a_typed_error(self) -> None:
        payload = json.dumps({"error": True, "code": "RATE_LIMITED", "message": "Rate limited."})
        with self._patch(CompletedProcess(stdout=payload, returncode=1)):
            with self.assertRaises(NotebookLMError) as ctx:
                self.cli.create_notebook("T")
        self.assertEqual(ctx.exception.code, CODE_RATE_LIMITED)
        self.assertTrue(ctx.exception.is_rate_limit)

    def test_auth_envelope_becomes_a_typed_error(self) -> None:
        payload = json.dumps({"error": True, "code": "AUTH_ERROR", "message": "expired"})
        with self._patch(CompletedProcess(stdout=payload, returncode=1)):
            with self.assertRaises(NotebookLMError) as ctx:
                self.cli.create_notebook("T")
        self.assertTrue(ctx.exception.is_auth)

    def test_nonzero_exit_without_json_still_raises(self) -> None:
        with self._patch(CompletedProcess(stderr="something broke", returncode=2)):
            with self.assertRaises(NotebookLMError) as ctx:
                self.cli.create_notebook("T")
        self.assertIn("something broke", str(ctx.exception))

    def test_local_timeout_on_short_command_is_typed(self) -> None:
        with mock.patch.object(
            nlm_cli.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(cmd="notebooklm", timeout=1),
        ):
            with self.assertRaises(NotebookLMError) as ctx:
                self.cli.create_notebook("T")
        self.assertEqual(ctx.exception.code, CODE_TIMEOUT)

    def test_missing_binary_names_the_install_command(self) -> None:
        with mock.patch.object(nlm_cli.subprocess, "run", side_effect=FileNotFoundError()):
            with self.assertRaises(NotebookLMNotInstalled) as ctx:
                self.cli.create_notebook("T")
        self.assertIn("uv tool install", str(ctx.exception))

    def test_auth_ok_is_a_passive_probe(self) -> None:
        with self._patch(CompletedProcess(stdout='{"ok": true}')) as run:
            ok, _ = self.cli.auth_ok()
        self.assertTrue(ok)
        argv = run.call_args[0][0]
        self.assertIn("--passive", argv)
        self.assertIn("--test", argv)

    def test_auth_ok_reports_false_instead_of_raising(self) -> None:
        payload = json.dumps({"error": True, "code": "AUTH_ERROR", "message": "expired"})
        with self._patch(CompletedProcess(stdout=payload, returncode=1)):
            ok, detail = self.cli.auth_ok()
        self.assertFalse(ok)
        self.assertIn("expired", detail)

    def test_download_verifies_the_file_actually_appeared(self) -> None:
        payload = json.dumps({"operation": "download_single", "output_path": "/nope/missing.m4a"})
        with self._patch(CompletedProcess(stdout=payload)):
            with self.assertRaises(NotebookLMError):
                self.cli.download_audio("nb_1", Path("/nope/missing.m4a"))

    def test_download_refuses_a_path_we_did_not_request(self) -> None:
        """The caller moves this file into podpub's inbox, so following the
        CLI to a surprise path would relocate an arbitrary file."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "wanted.m4a"
            dest.write_bytes(b"audio")
            elsewhere = Path(tmp) / "somewhere-else.m4a"
            elsewhere.write_bytes(b"other")
            payload = json.dumps({"output_path": str(elsewhere)})
            with self._patch(CompletedProcess(stdout=payload)):
                with self.assertRaises(NotebookLMError) as ctx:
                    self.cli.download_audio("nb_1", dest)
        self.assertIn("refusing to touch it", str(ctx.exception))

    def test_download_accepts_the_requested_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "wanted.m4a"
            dest.write_bytes(b"audio")
            payload = json.dumps({"output_path": str(dest)})
            with self._patch(CompletedProcess(stdout=payload)):
                self.assertEqual(self.cli.download_audio("nb_1", dest), dest)

    # ----- argv hygiene (M4) -----

    def test_title_starting_with_a_dash_is_passed_as_a_positional(self) -> None:
        payload = json.dumps({"notebook": {"id": "nb_x"}})
        with self._patch(CompletedProcess(stdout=payload)) as run:
            self.cli.create_notebook("-- weird --title")
        argv = run.call_args[0][0]
        self.assertEqual(argv[-1], "-- weird --title")
        self.assertEqual(argv[-2], "--", "positional must follow a bare --")
        self.assertLess(argv.index("--json"), argv.index("--"),
                        "click rejects options placed after --")

    def test_focus_prompt_travels_in_a_file_not_argv(self) -> None:
        """A prompt beginning with '-' would otherwise be parsed as an option."""
        payload = json.dumps({"task_id": "t1", "status": "pending"})
        captured: dict[str, str] = {}

        def fake_run(argv, **kwargs):
            path = Path(argv[argv.index("--prompt-file") + 1])
            captured["text"] = path.read_text(encoding="utf-8")
            return CompletedProcess(stdout=payload)

        with mock.patch.object(nlm_cli.subprocess, "run", side_effect=fake_run) as run:
            self.cli.start_audio("nb_1", "-- focus on the negative space")

        self.assertEqual(captured["text"], "-- focus on the negative space")
        self.assertNotIn("-- focus on the negative space", run.call_args[0][0])

    def test_missing_binary_is_not_reported_as_an_auth_problem(self) -> None:
        """auth_ok swallowing this made an uninstalled CLI look like expired
        cookies, sending the agent off to run `notebooklm login`."""
        with mock.patch.object(nlm_cli.subprocess, "run", side_effect=FileNotFoundError()):
            with self.assertRaises(NotebookLMNotInstalled):
                self.cli.auth_ok()
            with self.assertRaises(NotebookLMNotInstalled):
                self.cli.auth_refresh()

    def test_artifact_url_is_scrubbed_before_it_can_be_stored(self) -> None:
        payload = json.dumps({
            "task_id": "t1", "status": "completed",
            "url": "https://x/y.m4a?access_token=abc123def456ghi",
        })
        with self._patch(CompletedProcess(stdout=payload)):
            status = self.cli.poll("nb_1", "t1")
        self.assertNotIn("abc123def456ghi", status.url or "")


class VersionPinTest(unittest.TestCase):
    def test_matching_version(self) -> None:
        self.assertTrue(version_matches("NotebookLM CLI, version 0.8.0 (8fb61cb1)"))

    def test_other_versions_and_junk(self) -> None:
        self.assertFalse(version_matches("NotebookLM CLI, version 0.9.0"))
        self.assertFalse(version_matches(""))


if __name__ == "__main__":
    unittest.main()
