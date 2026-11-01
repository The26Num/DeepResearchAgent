"""Fail fast if another API instance owns the same SQLite database.

This OS file lock enforces the single-process API deployment boundary; it is
not a distributed job lease. CLI/evaluation must still be run separately.
The lock file can remain on disk: the OS releases its lock on process death.
"""

import os
from pathlib import Path


class ApiProcessLock:
    def __init__(self, db_path):
        path = Path(db_path).resolve()
        self._file = open(path.with_name(path.name + ".api.lock"), "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0, os.SEEK_END)
                if self._file.tell() == 0:
                    self._file.write(b"0")
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._file.close()
            raise RuntimeError("Only one API process may use a research database at a time.") from error

    def close(self):
        if self._file.closed:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
