"""Owned subprocess lifetime and local process identity (stdlib only)."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading


class MediaCancelled(BaseException):
    """An interrupted media command, not an encoding failure eligible for retry."""


def process_identity(pid: int) -> tuple[str, str | None]:
    """Return alive/dead/unknown and a creation identity, never infer death from age."""
    if pid <= 0:
        return "unknown", None
    if os.name == "nt":
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        h = k.OpenProcess(0x1000 | 0x100000, False, pid)
        if not h:
            return ("dead" if ctypes.get_last_error() == 87 else "unknown"), None
        try:
            if k.WaitForSingleObject(wintypes.HANDLE(h), 0) == 0:
                return "dead", None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k.GetProcessTimes(wintypes.HANDLE(h), *(ctypes.byref(t) for t in times)):
                return "unknown", None
            t = times[0]
            return "alive", str((t.dwHighDateTime << 32) | t.dwLowDateTime)
        finally:
            k.CloseHandle(wintypes.HANDLE(h))
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if stat[0] == "Z":
                return "dead", None
        except FileNotFoundError:
            return "dead", None
        except (OSError, IndexError):
            return "unknown", None
        try:
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return "alive", f"{boot}:{stat[19]}"
        except (OSError, IndexError):
            return "unknown", None
    try:
        os.kill(pid, 0)
        return "alive", None
    except ProcessLookupError:
        return "dead", None
    except OSError:
        return "unknown", None


class _WindowsJob:
    def __init__(self):
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [("user", ctypes.c_int64), ("job", ctypes.c_int64),
                        ("flags", w.DWORD), ("min", ctypes.c_size_t),
                        ("max", ctypes.c_size_t), ("active", w.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
                        ("scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in
                        ("read", "write", "other", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k.CreateJobObjectW.restype = w.HANDLE
        self.handle = self.k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.k.SetInformationJobObject(w.HANDLE(self.handle), 9,
                                             ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def spawn(self, child, parameters):
        """Create with an atomic job-list attribute (Windows 10+), without an attach gap."""
        from ctypes import wintypes as w
        class Startup(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR),
                        ("title", w.LPWSTR), ("x", w.DWORD), ("y", w.DWORD),
                        ("xsize", w.DWORD), ("ysize", w.DWORD), ("xchars", w.DWORD),
                        ("ychars", w.DWORD), ("fill", w.DWORD), ("flags", w.DWORD),
                        ("show", w.WORD), ("reserved_size", w.WORD),
                        ("reserved_bytes", ctypes.c_void_p), ("stdin", w.HANDLE),
                        ("stdout", w.HANDLE), ("stderr", w.HANDLE)]
        class StartupEx(ctypes.Structure):
            _fields_ = [("startup", Startup), ("attributes", ctypes.c_void_p)]
        class ProcessInfo(ctypes.Structure):
            _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE),
                        ("pid", w.DWORD), ("tid", w.DWORD)]
        cmd, _, _, _, _, cwd, env, _, _, _, *pipes = parameters
        if env is not None:
            raise ValueError("owned media commands use the inherited environment")
        p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite = pipes[:6]
        size = ctypes.c_size_t()
        k = self.k
        k.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        attributes = ctypes.create_string_buffer(size.value)
        if not k.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            handles = (w.HANDLE * 3)(int(p2cread), int(c2pwrite), int(errwrite))
            jobs = (w.HANDLE * 1)(self.handle)
            for attribute, value in ((0x20002, handles), (0x2000D, jobs)):
                if not k.UpdateProcThreadAttribute(attributes, 0, attribute, value,
                                                   ctypes.sizeof(value), None, None):
                    raise ctypes.WinError(ctypes.get_last_error())
            startup = StartupEx()
            startup.startup.cb = ctypes.sizeof(startup)
            startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = handles
            startup.attributes = ctypes.cast(attributes, ctypes.c_void_p)
            info = ProcessInfo()
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(cmd))
            k.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                        w.BOOL, w.DWORD, ctypes.c_void_p, w.LPCWSTR,
                                        ctypes.c_void_p, ctypes.c_void_p]
            if not k.CreateProcessW(None, command, None, None, True, 0x80000, None,
                                    str(cwd) if cwd is not None else None,
                                    ctypes.byref(startup), ctypes.byref(info)):
                error = ctypes.get_last_error()
                if error in (2, 3):
                    raise FileNotFoundError(error, "executable not found", cmd[0])
                raise ctypes.WinError(error)
            child._child_created = True
            child._handle = subprocess.Handle(info.process)
            child.pid = info.pid
            k.CloseHandle(w.HANDLE(info.thread))
        finally:
            k.DeleteProcThreadAttributeList(attributes)
            child._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)

    def close(self):
        from ctypes import wintypes as w
        if self.handle:
            self.k.CloseHandle(w.HANDLE(self.handle))
            self.handle = None


class _JobPopen(subprocess.Popen):
    def __init__(self, job, *args, **kwargs):
        self._job = job
        super().__init__(*args, **kwargs)

    def _execute_child(self, *parameters):
        self._job.spawn(self, parameters)


class ProcessController:
    """One process-wide scope shared by all pair worker threads."""
    def __init__(self):
        self.cancelled = threading.Event()
        self._condition = threading.Condition()
        self._children: set[subprocess.Popen] = set()
        self._closed = False
        self._job = _WindowsJob() if os.name == "nt" else None

    def run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        with self._condition:
            if self.cancelled.is_set() or self._closed:
                raise MediaCancelled()
            args = cmd
            if not self._job and sys.platform.startswith("linux"):
                # No preexec_fn in a threaded parent. The tiny single-threaded helper arms
                # PDEATHSIG before exec and checks for death during its own startup.
                if shutil.which(cmd[0]) is None:
                    raise FileNotFoundError(cmd[0])
                args = [sys.executable, str(Path(__file__).resolve()), str(os.getpid()), *cmd]
            options = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
            child = (_JobPopen(self._job, args, **options) if self._job
                     else subprocess.Popen(args, **options))
            self._children.add(child)
        try:
            stdout, stderr = child.communicate()
            if self.cancelled.is_set():
                raise MediaCancelled()
            return subprocess.CompletedProcess(cmd, child.returncode, stdout, stderr)
        except BaseException:
            self.cancel()
            child.communicate()
            raise
        finally:
            with self._condition:
                self._children.discard(child)
                self._condition.notify_all()

    def cancel(self) -> None:
        with self._condition:
            self.cancelled.set()
            if self._job and self._job.handle:
                from ctypes import wintypes as w
                if not self._job.k.TerminateJobObject(w.HANDLE(self._job.handle), 1):
                    raise ctypes.WinError(ctypes.get_last_error())
            for child in self._children:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass

    def close(self) -> None:
        self.cancel()
        with self._condition:
            self._closed = True
            while self._children:
                self._condition.wait()
            if self._job:
                self._job.close()


if __name__ == "__main__":
    parent = int(sys.argv[1])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot arm parent-death signal")
    if os.getppid() != parent:
        sys.exit(125)
    os.execvp(sys.argv[2], sys.argv[2:])
