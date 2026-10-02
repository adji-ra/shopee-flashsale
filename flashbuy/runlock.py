"""Lock antar-proses: hanya satu run/precheck/doctor/kalibrasi yang memegang browser & HP pada satu waktu.

File `~/.flashbuy/run.lock` dikunci eksklusif tanpa menunggu (POSIX flock / Windows msvcrt). Lock dilepas OS
saat proses keluar (termasuk crash), jadi tidak ada lock basi. Info pemegang (PID, perintah, waktu) ditulis ke
`run.lock.json` supaya proses kedua bisa ditolak dengan pesan jelas. `FLASHBUY_HOME` mengganti `~/.flashbuy`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


class RunLockBusy(RuntimeError):
    pass


def home_dir() -> Path:
    return Path(os.environ.get("FLASHBUY_HOME") or Path.home() / ".flashbuy")


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class RunLock:
    def __init__(self, command: str, path: Path | None = None):
        self.command = command
        self.path = path or home_dir() / "run.lock"
        self.info_path = self.path.with_name(self.path.name + ".json")
        self._fd: int | None = None

    def acquire(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        if not _try_lock(fd):
            os.close(fd)
            raise RunLockBusy(self.busy_message())
        self._fd = fd
        info = {"pid": os.getpid(), "command": self.command, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        try:
            self.info_path.write_text(json.dumps(info), encoding="utf-8")
        except OSError:
            pass
        return self

    def busy_message(self) -> str:
        try:
            info = json.loads(self.info_path.read_text(encoding="utf-8"))
            who = f"PID {info.get('pid')} ({info.get('command')}, mulai {info.get('started')})"
        except (OSError, ValueError):
            who = "proses lain"
        return (f"flashbuy lain sedang berjalan: {who}. Hanya satu run/precheck/doctor/kalibrasi boleh memegang "
                f"browser & HP sekaligus; tunggu selesai atau hentikan proses itu (lock: {self.path}).")

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            self.info_path.unlink(missing_ok=True)
        except OSError:
            pass
        _unlock(self._fd)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> RunLock:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
