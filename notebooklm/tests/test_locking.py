"""Lockfile: acquire, release, respect a live holder, reclaim a dead one."""

from __future__ import annotations

import json
import multiprocessing
import os
import socket
import tempfile
import unittest
from pathlib import Path

from notebooklm.locking import LockBusyError, PipelineLock, read_lock

RACERS = 8


def _race_worker(path_str: str, barrier, results, holders, counter_lock) -> None:
    """Grab the lock at the same instant as every sibling and report whether
    anyone else was inside the critical section at the same time.

    Serialized re-acquisition is fine and expected (each winner releases), so
    the invariant under test is mutual exclusion, not "exactly one winner".
    """
    import time

    from notebooklm.locking import LockBusyError as Busy
    from notebooklm.locking import PipelineLock as Lock

    barrier.wait()
    try:
        lock = Lock(Path(path_str)).acquire()
    except Busy:
        results.put("busy")
        return
    except Exception as exc:  # a crash is a bug, not a clean "someone else has it"
        results.put(f"crash: {type(exc).__name__}: {exc}")
        return

    with counter_lock:
        holders.value += 1
        concurrent = holders.value
    time.sleep(0.05)
    with counter_lock:
        holders.value -= 1

    lock.release()
    results.put("won" if concurrent == 1 else f"OVERLAP: {concurrent} holders at once")


def _dead_pid() -> int:
    """A PID that is almost certainly not running."""
    pid = 999_999
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pass
        pid -= 1


class LockingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".lock"
        self.addCleanup(self.tmp.cleanup)

    def _write_lock(self, pid: int, host: str | None = None) -> None:
        payload = {"pid": pid, "host": host or socket.gethostname(), "started_at": "now"}
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_acquire_creates_lockfile_with_our_pid(self) -> None:
        with PipelineLock(self.path):
            info = read_lock(self.path)
            assert info is not None
            self.assertEqual(info.pid, os.getpid())
        self.assertFalse(self.path.exists(), "lock must be released on exit")

    def test_lock_is_released_even_when_the_body_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            with PipelineLock(self.path):
                raise RuntimeError("boom")
        self.assertFalse(self.path.exists())

    def test_live_holder_blocks_a_second_run(self) -> None:
        self._write_lock(os.getppid() or os.getpid())
        with self.assertRaises(LockBusyError) as ctx:
            PipelineLock(self.path).acquire()
        self.assertIn("already running", str(ctx.exception))

    def test_busy_message_warns_about_podpub(self) -> None:
        self._write_lock(os.getppid() or os.getpid())
        with self.assertRaises(LockBusyError) as ctx:
            PipelineLock(self.path).acquire()
        self.assertIn("podpub.py", str(ctx.exception))

    def test_stale_lock_from_a_dead_pid_is_reclaimed(self) -> None:
        self._write_lock(_dead_pid())
        lock = PipelineLock(self.path).acquire()
        self.assertTrue(lock.reclaimed_stale)
        lock.release()

    def test_unreadable_lockfile_is_treated_as_held_not_stale(self) -> None:
        """A half-synced lockfile on a Drive volume is more likely a live run
        than a dead one, so we refuse rather than stealing it."""
        self.path.write_text("not json at all", encoding="utf-8")
        with self.assertRaises(LockBusyError) as ctx:
            PipelineLock(self.path).acquire()
        self.assertIn("cannot be read", str(ctx.exception))

    def test_lock_missing_required_fields_is_held(self) -> None:
        self.path.write_text(json.dumps({"started_at": "now"}), encoding="utf-8")
        with self.assertRaises(LockBusyError):
            PipelineLock(self.path).acquire()

    def test_foreign_host_lock_is_never_reclaimed_even_with_a_dead_pid(self) -> None:
        """PIDs are not comparable across machines sharing a Drive folder."""
        self._write_lock(_dead_pid(), host="some-other-mac")
        with self.assertRaises(LockBusyError) as ctx:
            PipelineLock(self.path).acquire()
        self.assertIn("different machine", str(ctx.exception))

    def test_concurrent_processes_never_hold_the_lock_at_once(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(RACERS)
        queue = ctx.Queue()
        holders = ctx.Value("i", 0)
        counter_lock = ctx.Lock()
        procs = [
            ctx.Process(target=_race_worker,
                        args=(str(self.path), barrier, queue, holders, counter_lock))
            for _ in range(RACERS)
        ]
        for proc in procs:
            proc.start()
        outcomes = [queue.get(timeout=60) for _ in range(RACERS)]
        for proc in procs:
            proc.join(timeout=60)

        self.assertEqual([o for o in outcomes if o.startswith("crash")], [],
                         f"losing a lock race must raise LockBusyError, not crash: {outcomes}")
        self.assertEqual([o for o in outcomes if o.startswith("OVERLAP")], [],
                         f"the lock did not provide mutual exclusion: {outcomes}")
        self.assertGreaterEqual(outcomes.count("won"), 1, f"nobody acquired the lock: {outcomes}")

    def test_read_lock_on_free_lock_returns_none(self) -> None:
        self.assertIsNone(read_lock(self.path))

    def test_release_leaves_another_holders_lock_alone(self) -> None:
        lock = PipelineLock(self.path).acquire()
        self._write_lock(_dead_pid())  # someone else took over the file
        lock.release()
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main()
