"""Real subprocess tests for scope ownership and restartable local run locks."""
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from rg2019 import media
from rg2019 import state as state_module
from rg2019.processes import process_identity
from rg2019.state import RunLock

ROOT = Path(__file__).resolve().parents[1]
OWNED_PLATFORMS = os.name == "nt" or sys.platform.startswith("linux")


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    pytest.fail("timed out waiting for subprocess lifecycle condition")


def mock_atomic_os(monkeypatch, replace, fsync, platform="nt"):
    monkeypatch.setattr(state_module, "os", SimpleNamespace(
        name=platform, getpid=os.getpid, replace=replace, fsync=fsync))


@pytest.mark.parametrize("failures,winerror", [(1, 5), (3, 32), (5, 33)])
def test_atomic_write_retries_transient_windows_replace_only(
        tmp_path, monkeypatch, caplog, failures, winerror):
    destination = tmp_path / "state.json"
    destination.write_text("previous state")
    calls, synced, sleeps = [], [], []
    original_replace, original_fsync = os.replace, os.fsync
    def replace(source, target):
        calls.append((source, target))
        assert synced, "the staged state must be fsynced before publication"
        if len(calls) <= failures:
            error = PermissionError("transient Windows sharing/access violation")
            error.winerror = winerror
            raise error
        original_replace(source, target)
    def fsync(fd):
        synced.append(fd)
        original_fsync(fd)
    mock_atomic_os(monkeypatch, replace, fsync)
    monkeypatch.setattr(state_module, "time", SimpleNamespace(sleep=sleeps.append), raising=False)
    state_module.atomic_write_text(destination, "new state\n")
    assert destination.read_text() == "new state\n"
    assert len(synced) == 1
    assert len(calls) == failures + 1
    expected_source = destination.with_name(destination.name + f".{os.getpid()}.tmp")
    assert all(pair == (expected_source, destination) for pair in calls)
    assert len(sleeps) == failures and sum(sleeps) <= 2
    assert len([r for r in caplog.records if "retry" in r.message.lower()]) == failures
    assert not expected_source.exists()


def test_atomic_write_windows_replace_retry_exhaustion_propagates(
        tmp_path, monkeypatch, caplog):
    destination = tmp_path / "state.json"
    destination.write_text("previous state")
    error = PermissionError("persistent Windows sharing violation")
    error.winerror = 32
    calls, synced, sleeps = [], [], []
    def replace(source, target):
        calls.append((source, target))
        raise error
    mock_atomic_os(monkeypatch, replace, synced.append)
    monkeypatch.setattr(state_module, "time", SimpleNamespace(sleep=sleeps.append), raising=False)
    with pytest.raises(PermissionError) as raised:
        state_module.atomic_write_text(destination, "new state\n")
    assert raised.value is error
    assert destination.read_text() == "previous state"
    assert len(synced) == 1
    assert len(calls) == len(sleeps) + 1
    assert len(calls) == 6
    assert 0 < sum(sleeps) <= 2
    assert len([r for r in caplog.records if "retry" in r.message.lower()]) == len(sleeps)
    expected_source = destination.with_name(destination.name + f".{os.getpid()}.tmp")
    assert all(pair == (expected_source, destination) for pair in calls)
    assert expected_source.read_text() == "new state\n"


@pytest.mark.parametrize("platform,winerror", [("nt", None), ("nt", 2), ("posix", 5)])
def test_atomic_write_other_replace_errors_are_immediate(tmp_path, monkeypatch, platform, winerror):
    destination = tmp_path / "state.json"
    destination.write_text("previous state")
    error = PermissionError("not a retryable Windows error")
    if winerror is not None:
        error.winerror = winerror
    calls, sleeps, synced = [], [], []
    def replace(source, target):
        calls.append((source, target))
        raise error
    mock_atomic_os(monkeypatch, replace, synced.append, platform)
    monkeypatch.setattr(state_module, "time", SimpleNamespace(sleep=sleeps.append), raising=False)
    with pytest.raises(PermissionError) as raised:
        state_module.atomic_write_text(destination, "new state")
    assert raised.value is error
    assert len(calls) == len(synced) == 1
    assert not sleeps
    assert destination.read_text() == "previous state"


def test_atomic_write_does_not_retry_fsync_errors(tmp_path, monkeypatch):
    destination = tmp_path / "state.json"
    destination.write_text("previous state")
    error = PermissionError("fsync failed")
    error.winerror = 5
    calls, sleeps = [], []
    def fsync(fd):
        raise error
    mock_atomic_os(monkeypatch, lambda *args: calls.append(args), fsync)
    monkeypatch.setattr(state_module, "time", SimpleNamespace(sleep=sleeps.append), raising=False)
    with pytest.raises(PermissionError) as raised:
        state_module.atomic_write_text(destination, "new state")
    assert raised.value is error
    assert not calls and not sleeps
    assert destination.read_text() == "previous state"


def writer_command(marker):
    return [sys.executable, "-c",
            "import os,sys,time; from pathlib import Path; "
            "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(120)", str(marker)]


@pytest.mark.parametrize("scoped", [False, True])
def test_run_preserves_completed_process_and_argument_boundaries(scoped):
    command = [sys.executable, "-c", "import sys; sys.stdout.reconfigure(encoding='utf-8'); "
               "print(repr(sys.argv[1:]))",
               "a b", "quote\"inside", "α; & echo not-a-shell"]
    if scoped:
        with media.process_scope():
            result = media._run(command, "probe")
    else:
        result = media._run(command, "probe")
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.args == command
    assert result.returncode == 0
    assert repr(command[3:]) in result.stdout


def test_scoped_errors_preserve_media_error():
    with media.process_scope():
        with pytest.raises(media.MediaError, match="executable not found"):
            media._run(["rg2019-no-such-executable-42"], "probe")
        with pytest.raises(media.MediaError, match=r"failed \(exit 7\).*bad media"):
            media._run([sys.executable, "-c",
                        "import sys; print('bad media', file=sys.stderr); sys.exit(7)"], "probe")


def test_cancelled_copy_is_not_retried_or_deleted(tmp_path, cfg, monkeypatch):
    source, partial = tmp_path / "source.mp4", tmp_path / "output.partial"
    partial.write_bytes(b"retain interruption checkpoint")
    calls = []
    def interrupted(cmd, what):
        calls.append(cmd)
        raise media.MediaCancelled()
    monkeypatch.setattr(media, "_run", interrupted)
    info = media.MediaInfo(1, 1.0, "mp4", 0.0, video_codec="h264", audio_codec="aac")
    with pytest.raises(media.MediaCancelled):
        media.encode(cfg, source, partial, 0.0, info)
    assert len(calls) == 1
    assert partial.read_bytes() == b"retain interruption checkpoint"


def test_cancellation_reaps_all_workers_and_prevents_new_commands(tmp_path):
    markers = [tmp_path / f"child-{i}" for i in range(3)]
    with media.process_scope() as scope:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(media._run, writer_command(marker), "writer")
                       for marker in markers]
            try:
                wait_for(lambda: all(p.exists() and p.read_text() for p in markers))
                media.cancel_processes()
                assert scope.cancelled.is_set()
                for future in futures:
                    with pytest.raises(media.MediaCancelled):
                        future.result(timeout=10)
                with pytest.raises(media.MediaCancelled):
                    media._run(writer_command(tmp_path / "never-started"), "writer")
                for marker in markers:
                    wait_for(lambda: process_identity(int(marker.read_text()))[0] == "dead")
                assert not (tmp_path / "never-started").exists()
            finally:
                scope.cancel()
    assert not issubclass(media.MediaCancelled, Exception)


def test_scope_exit_reaps_commands_before_returning(tmp_path):
    marker = tmp_path / "child"
    failures = []
    def worker():
        try:
            media._run(writer_command(marker), "writer")
        except media.MediaCancelled:
            failures.append("cancelled")
    with media.process_scope():
        thread = threading.Thread(target=worker)
        thread.start()
        wait_for(lambda: marker.exists() and marker.read_text())
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert failures == ["cancelled"]
    wait_for(lambda: process_identity(int(marker.read_text()))[0] == "dead")
    with media.process_scope():
        assert media._run([sys.executable, "-c", "print('restart')"], "probe").stdout == "restart\n"


@pytest.mark.skipif(not OWNED_PLATFORMS, reason="hard parent-death protection is Windows/Linux")
def test_hard_parent_termination_kills_writer_and_recovers_fresh_lock(tmp_path):
    marker, lock_path = tmp_path / "writer.pid", tmp_path / "pipeline.lock"
    code = ("import sys; from pathlib import Path; from rg2019 import media; "
            "from rg2019.state import RunLock; "
            "lock=RunLock(Path(sys.argv[1]),24); lock.acquire(); "
            "scope=media.process_scope(); scope.__enter__(); "
            "media._run([sys.executable,'-c',sys.argv[3],sys.argv[2]],'writer')")
    child_code = writer_command(marker)[2]
    parent = subprocess.Popen([sys.executable, "-c", code, str(lock_path), str(marker), child_code],
                              cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child_pid = None
    try:
        wait_for(lambda: marker.exists() and marker.read_text() or parent.poll() is not None)
        if parent.poll() is not None:
            pytest.fail(f"parent failed to start writer: {parent.communicate()[1]!r}")
        child_pid = int(marker.read_text())
        with pytest.raises(RuntimeError, match="another pipeline run"):
            RunLock(lock_path, 24).acquire()
        parent.kill()
        parent.wait(timeout=10)
        wait_for(lambda: process_identity(child_pid)[0] == "dead")
        assert time.time() - lock_path.stat().st_mtime < 60
        with RunLock(lock_path, 24) as restarted:
            assert restarted.held
            record = json.loads(lock_path.read_text())
            assert record["pid"] == os.getpid()
            assert record["identity"] == process_identity(os.getpid())[1]
        assert not lock_path.exists()
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.communicate(timeout=10)
        if child_pid is not None and process_identity(child_pid)[0] == "alive":
            os.kill(child_pid, 9)


@pytest.mark.skipif(not OWNED_PLATFORMS, reason="hard parent-death protection is Windows/Linux")
def test_hard_parent_termination_stops_real_ffmpeg_before_lock_recovery(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None, "the test suite's managed FFmpeg must be available"
    marker = tmp_path / "ffmpeg.pid"
    output = tmp_path / "synthetic.rgb"
    lock_path = tmp_path / "pipeline.lock"
    command = [ffmpeg, "-nostdin", "-y", "-v", "error", "-re", "-f", "lavfi",
               "-i", "testsrc=size=64x64:rate=10", "-t", "120", "-c:v", "rawvideo",
               "-threads", "1", "-pix_fmt", "rgb24", "-flush_packets", "1",
               "-f", "rawvideo", str(output)]
    code = """
import json, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from rg2019 import media
from rg2019.state import RunLock
with RunLock(Path(sys.argv[1]), 24):
    with media.process_scope() as scope:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(media._run, json.loads(sys.argv[3]), "synthetic FFmpeg writer")
            while True:
                with scope._condition:
                    if scope._children:
                        Path(sys.argv[2]).write_text(str(next(iter(scope._children)).pid))
                        break
                if future.done():
                    future.result()
                    raise RuntimeError("FFmpeg exited before its PID was recorded")
                time.sleep(0.01)
            future.result()
"""
    parent = subprocess.Popen([sys.executable, "-c", code, str(lock_path), str(marker),
                               json.dumps(command)], cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    writer_pid = None
    try:
        wait_for(lambda: (marker.exists() and marker.read_text() and output.exists()
                          and output.stat().st_size > 0) or parent.poll() is not None)
        if parent.poll() is not None:
            pytest.fail(f"synthetic FFmpeg failed to start: {parent.communicate()[1]!r}")
        writer_pid = int(marker.read_text())
        assert process_identity(writer_pid)[0] == "alive"
        with pytest.raises(RuntimeError, match="another pipeline run"):
            RunLock(lock_path, 24).acquire()
        parent.kill()
        parent.wait(timeout=10)
        wait_for(lambda: process_identity(writer_pid)[0] == "dead")
        settled = (output.stat().st_size, output.stat().st_mtime_ns)
        time.sleep(0.35)
        assert (output.stat().st_size, output.stat().st_mtime_ns) == settled
        assert time.time() - lock_path.stat().st_mtime < 60
        with RunLock(lock_path, 24):
            assert process_identity(writer_pid)[0] == "dead"
            assert json.loads(lock_path.read_text())["pid"] == os.getpid()
        assert not lock_path.exists()
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.communicate(timeout=10)
        if writer_pid is not None and process_identity(writer_pid)[0] == "alive":
            os.kill(writer_pid, 9)


def test_live_owner_respected_even_without_heartbeat(tmp_path):
    lock_path = tmp_path / "pipeline.lock"
    first = RunLock(lock_path, 24)
    first.acquire()
    try:
        first._stop.set()
        first._thread.join(timeout=5)
        old = time.time() - 48 * 3600
        os.utime(lock_path, (old, old))
        with pytest.raises(RuntimeError, match="another pipeline run"):
            RunLock(lock_path, 24).acquire()
    finally:
        first.release()


@pytest.mark.parametrize("owner", ["pid-reused", "foreign", "unknown"])
def test_owner_identity_recovery_is_conservative(tmp_path, monkeypatch, owner):
    lock_path = tmp_path / "pipeline.lock"
    first = RunLock(lock_path, 24)
    first.acquire()
    first._stop.set()
    first._thread.join(timeout=5)
    record = json.loads(lock_path.read_text())
    record["identity"] = "a-different-creation-identity"
    if owner == "foreign":
        record["host"] = socket.gethostname() + "-other"
    if owner == "unknown":
        monkeypatch.setattr("rg2019.state.process_identity", lambda _: ("unknown", None))
    lock_path.write_text(json.dumps(record))
    old = time.time() - 48 * 3600
    os.utime(lock_path, (old, old))
    try:
        second = RunLock(lock_path, 24)
        if owner == "pid-reused":
            second.acquire()
            try:
                replacement = lock_path.read_text()
                first.release()
                assert lock_path.read_text() == replacement
            finally:
                second.release()
        else:
            with pytest.raises(RuntimeError):
                second.acquire()
    finally:
        first.release()


def test_losing_token_does_not_touch_or_remove_new_owners_lock(tmp_path):
    lock_path = tmp_path / "pipeline.lock"
    first = RunLock(lock_path, 1)
    first.acquire()
    first._stop.set()
    first._thread.join(timeout=5)
    record = json.loads(lock_path.read_text())
    record["token"] = "f" * 32
    lock_path.write_text(json.dumps(record))
    original_mtime = lock_path.stat().st_mtime_ns
    first.interval = 0.01
    first._stop.clear()
    first._heartbeat()
    assert lock_path.stat().st_mtime_ns == original_mtime
    first.release()
    assert json.loads(lock_path.read_text())["token"] == "f" * 32


@pytest.mark.parametrize("existing", [None, b"", b"\0"])
def test_guard_initialization_is_protected_by_os_lock(tmp_path, monkeypatch, existing):
    lock = RunLock(tmp_path / "pipeline.lock", 24)
    guard = tmp_path / "pipeline.lock.guard"
    if existing is not None:
        guard.write_bytes(existing)
    locked = False
    writes = []
    if os.name == "nt":
        import msvcrt
        original_lock = msvcrt.locking
        def locking(fd, mode, count):
            nonlocal locked
            original_lock(fd, mode, count)
            locked = mode != msvcrt.LK_UNLCK
        monkeypatch.setattr(msvcrt, "locking", locking)
    else:
        import fcntl
        original_lock = fcntl.flock
        def flock(fd, mode):
            nonlocal locked
            original_lock(fd, mode)
            locked = mode != fcntl.LOCK_UN
        monkeypatch.setattr(fcntl, "flock", flock)

    original_open = open
    class CheckedGuard:
        def __init__(self, fh):
            self.fh = fh
        def __getattr__(self, name):
            return getattr(self.fh, name)
        def __enter__(self):
            self.fh.__enter__()
            return self
        def __exit__(self, *exc):
            return self.fh.__exit__(*exc)
        def write(self, data):
            assert locked, "guard initialization must not race with an OS-locked contender"
            writes.append(data)
            return self.fh.write(data)
        def flush(self):
            assert locked, "guard initialization must be flushed before unlocking"
            return self.fh.flush()

    def checked_open(path, *args, **kwargs):
        assert path == guard
        return CheckedGuard(original_open(path, *args, **kwargs))
    monkeypatch.setattr(state_module, "open", checked_open, raising=False)
    with lock._guard():
        assert locked
    assert not locked
    assert guard.read_bytes() == b"\0"
    assert writes == ([] if existing == b"\0" else [b"\0"])
    with lock._guard():
        assert locked
    assert not locked
    assert guard.read_bytes() == b"\0"
    assert writes == ([] if existing == b"\0" else [b"\0"])


def test_competing_reclaims_allow_only_one_owner(tmp_path):
    lock_path = tmp_path / "pipeline.lock"
    lock_path.write_text("legacy abandoned lock")
    old = time.time() - 48 * 3600
    os.utime(lock_path, (old, old))
    barrier = threading.Barrier(4)
    locks = [RunLock(lock_path, 24) for _ in range(4)]
    def contender(lock):
        barrier.wait(timeout=5)
        try:
            lock.acquire()
            return True
        except RuntimeError:
            return False
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(contender, locks))
        assert results.count(True) == 1
        assert json.loads(lock_path.read_text())["token"] == locks[results.index(True)]._token
    finally:
        for lock in locks:
            lock.release()
