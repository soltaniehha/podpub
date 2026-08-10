"""`artifact poll` / `artifact wait` payloads exactly as notebooklm-py 0.8.0 emits them.

The other CLI tests use hand-written payloads carrying only the keys the wrapper
reads. The real command emits more than that: `cli/artifact_cmd.py:509-520`
(poll) and `:595-630` (wait) always include an `"error"` key - `None` on
success, a string when generation failed or the wait timed out - and the wait
JSON path calls `exit_with_code(1)` for *any* non-completed status.

`_run()` used to treat any truthy `payload["error"]` as the `{"error": true,
"code": ...}` failure envelope, so a status payload never reached
`_to_status()`: a wait timeout and a failed generation both surfaced as
`[CLI_ERROR] unknown error`. It now takes `status_payload=True` for these two
commands, where only `error is True` is an envelope and a non-zero exit
alongside a status report is expected rather than fatal. The truthy check
remains everywhere else - `download`'s legacy shape really is
`{"error": "<msg>"}` - and the envelope test below guards that boundary.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from notebooklm import nlm_cli
from notebooklm.nlm_cli import CODE_RATE_LIMITED, NotebookLMCLI, NotebookLMError


class CompletedProcess:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def poll_payload(status: str, error: str | None = None) -> str:
    """Mirror of the poll --json response (all six keys, always)."""
    return json.dumps({
        "task_id": "task_1",
        "status": status,
        "url": "https://x/y.m4a" if status == "completed" else None,
        "error": error,
        "error_code": "GEN_FAILED" if error else None,
        "metadata": {},
    })


def wait_payload(status: str, error: str | None = None) -> str:
    """Mirror of the wait --json response (note: artifact_id, not task_id)."""
    return json.dumps({
        "artifact_id": "task_1",
        "status": status,
        "url": "https://x/y.m4a" if status == "completed" else None,
        "error": error,
    })


class RealPayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = NotebookLMCLI(binary="notebooklm")

    def _patch(self, result):
        return mock.patch.object(nlm_cli.subprocess, "run", return_value=result)

    def test_successful_poll_with_a_null_error_key_parses(self) -> None:
        with self._patch(CompletedProcess(stdout=poll_payload("completed"))):
            status = self.cli.poll("nb_1", "task_1")
        self.assertTrue(status.is_complete)
        self.assertEqual(status.url, "https://x/y.m4a")

    def test_successful_wait_with_a_null_error_key_parses(self) -> None:
        with self._patch(CompletedProcess(stdout=wait_payload("completed"))):
            status = self.cli.wait("nb_1", "task_1", timeout=60)
        self.assertTrue(status.is_complete)

    def test_a_genuine_error_envelope_still_raises(self) -> None:
        """The fix must not stop recognizing real failure envelopes."""
        envelope = json.dumps({"error": True, "code": "RATE_LIMITED", "message": "slow down"})
        with self._patch(CompletedProcess(stdout=envelope, returncode=1)):
            with self.assertRaises(NotebookLMError) as ctx:
                self.cli.poll("nb_1", "task_1")
        self.assertEqual(ctx.exception.code, CODE_RATE_LIMITED)

    def test_failed_generation_is_a_status_not_a_cli_error(self) -> None:
        """README: 'Generation fails server-side -> recorded as failed with the
        error.' Today the reason is replaced by the string 'unknown error'."""
        with self._patch(CompletedProcess(stdout=poll_payload("failed", "model unavailable"))):
            status = self.cli.poll("nb_1", "task_1")
        self.assertTrue(status.is_failed)
        self.assertIn("model unavailable", status.error or "")

    def test_wait_timeout_reports_pending_against_the_real_payload(self) -> None:
        """README: 'Wait times out -> NOT an error. Next run polls.' The real
        CLI emits status=timeout with an error string and exits 1."""
        with self._patch(CompletedProcess(stdout=wait_payload("timeout", "Timed out after 1800 seconds"),
                                          returncode=1)):
            status = self.cli.wait("nb_1", "task_1", timeout=1800)
        self.assertFalse(status.is_failed)
        self.assertEqual(status.status, nlm_cli.STATUS_PENDING)

    def test_wait_still_pending_is_not_a_failure(self) -> None:
        """A non-completed wait exits 1 even with error=None."""
        with self._patch(CompletedProcess(stdout=wait_payload("pending"), returncode=1)):
            status = self.cli.wait("nb_1", "task_1", timeout=1800)
        self.assertFalse(status.is_failed)
        self.assertEqual(status.status, nlm_cli.STATUS_PENDING)


if __name__ == "__main__":
    unittest.main()
