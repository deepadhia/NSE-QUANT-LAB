"""Portfolio serialisation for overlapping workers.

Two layers, both crash-released:
* ``account_lock``: thread + OS file lock, serialises workers on ONE machine (fast, no network).
* ``PortfolioLease`` (lease.py): a MongoDB lease with a fencing token, serialises workers across HOSTS that share one database.
Database cash writes additionally use compare-and-swap, and call ``assert_held`` on the lease first.
"""
from contextlib import contextmanager
from functools import wraps
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time

_guard = threading.Lock()
_locks = {}


@contextmanager
def account_lock(identity: str, timeout: float = 5.0):
    key = hashlib.sha256(identity.encode()).hexdigest()
    with _guard:
        lock = _locks.setdefault(key, threading.Lock())
    if not lock.acquire(timeout=timeout):
        raise RuntimeError("ACCOUNT_BUSY: another worker owns this portfolio")
    handle = None
    acquired = False
    try:
        directory = Path(tempfile.gettempdir()) / "nse_quant_lab_locks"
        directory.mkdir(exist_ok=True)
        handle = (directory / (key + ".lock")).open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("ACCOUNT_BUSY: another process owns this portfolio")
                time.sleep(.05)
        yield
    finally:
        if handle is not None:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        lock.release()


def serialized_portfolio(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        identity = "|".join([self.config.mongo_uri, self.config.db_name,
                             self.config.strategy_id, self._mode])
        with account_lock(identity):
            if getattr(self, "_lease_depth", 0) > 0:      # re-entrant call inside an already-leased operation
                return fn(self, *args, **kwargs)
            lease = self._open_lease()
            if lease is not None:
                lease.acquire()
            self._lease, self._lease_depth = lease, 1
            try:
                return fn(self, *args, **kwargs)
            finally:
                self._lease_depth = 0
                self._lease = None
                if lease is not None:
                    lease.release()
    return wrapped
