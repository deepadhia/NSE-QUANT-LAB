"""Cross-host portfolio lease stored in MongoDB.

The OS file lock in locking.py only serialises workers on ONE machine. Two hosts (for example the Oracle server cron and a laptop
daemon) pointed at the same Atlas database would otherwise run the same strategy's fills at the same time. This lease makes the
database itself the arbiter:

* one document per ``strategy_id|mode`` in ``portfolio_leases`` (``_id`` = key), so acquisition is a single atomic upsert;
* an acquirer wins only if the lease is expired or already its own; if another live owner holds it, the upsert collides on ``_id``
  (DuplicateKeyError) and acquisition fails with ACCOUNT_BUSY;
* every acquisition increments a monotonic fencing ``token``; ``assert_held`` (called before every state-changing write) renews the
  lease only for the exact owner+token pair and raises LEASE_LOST if another worker has taken over, so a paused/slow worker
  that lost its lease can never keep writing;
* expiry is judged on the MongoDB SERVER clock (``hello.localTime``), never on host clocks, so clock skew between hosts cannot
  create two owners; a crashed owner's lease simply expires after ``ttl`` seconds.

It complements, and does not replace, the cash compare-and-swap in ``PaperTradingManager._persist_cash``.
"""
from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

try:
    from pymongo import ReturnDocument
    from pymongo.errors import DuplicateKeyError
except Exception:  # pragma: no cover - pymongo is a hard dependency in production
    ReturnDocument = None

    class DuplicateKeyError(Exception):
        pass

logger = logging.getLogger(__name__)

COLLECTION = "portfolio_leases"
DEFAULT_TTL_SECONDS = 120.0


class LeaseBusy(RuntimeError):
    """Another live owner holds the lease."""


class LeaseLost(RuntimeError):
    """This worker's lease expired and was taken over (or is otherwise no longer ours): stop writing."""


def default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"  # identity, not an idempotency key


def mongo_server_now(collection) -> datetime:
    """Current time on the MongoDB server (naive UTC, as BSON returns it); falls back to the local UTC clock only if unavailable."""
    try:
        return collection.database.client.admin.command("hello")["localTime"].replace(tzinfo=None)
    except Exception as exc:
        logger.warning("[LEASE_CLOCK_FALLBACK] could not read the MongoDB server clock (%s); using the local UTC clock", exc)
        return datetime.now(timezone.utc).replace(tzinfo=None)


def supports_leases(collection) -> bool:
    """True for a real pymongo collection. Lightweight in-memory test doubles lack find_one_and_update and cannot arbitrate."""
    return collection is not None and hasattr(collection, "find_one_and_update") and hasattr(collection, "database")


class PortfolioLease:
    def __init__(self, collection, key: str, ttl: float = DEFAULT_TTL_SECONDS, owner: Optional[str] = None,
                 now: Optional[Callable[[], datetime]] = None):
        self.coll = collection
        self.key = key
        self.ttl = float(ttl)
        self.owner = owner or default_owner()
        self._now = now or (lambda: mongo_server_now(collection))
        self.token: Optional[int] = None

    # ------------------------------------------------------------------ acquire / renew / release
    def _try_acquire(self) -> bool:
        now = self._now()
        try:
            doc = self.coll.find_one_and_update(
                {"_id": self.key, "$or": [{"expires_at": {"$lte": now}}, {"owner": self.owner}]},
                {"$set": {"owner": self.owner, "acquired_at": now, "expires_at": now + timedelta(seconds=self.ttl),
                          "host": socket.gethostname(), "pid": os.getpid()},
                 "$inc": {"token": 1}},
                upsert=True,
                return_document=ReturnDocument.AFTER if ReturnDocument is not None else True,
            )
        except DuplicateKeyError:
            return False            # another live owner: the upsert tried to insert a second document with this _id
        self.token = int(doc["token"])
        return True

    def acquire(self, timeout: float = 5.0, poll: float = 0.2) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if self._try_acquire():
                logger.info("[LEASE_ACQUIRED] %s token=%s owner=%s", self.key, self.token, self.owner)
                return
            if time.monotonic() >= deadline:
                holder = self.coll.find_one({"_id": self.key}) or {}
                raise LeaseBusy(f"ACCOUNT_BUSY: another host owns {self.key} "
                                f"(owner={holder.get('owner')}, expires_at={holder.get('expires_at')})")
            time.sleep(poll)

    def assert_held(self) -> None:
        """Verify we still own the lease at our token and extend it. Call before every state-changing write."""
        if self.token is None:
            raise LeaseLost(f"LEASE_LOST: {self.key} was never acquired")
        now = self._now()
        res = self.coll.update_one({"_id": self.key, "owner": self.owner, "token": self.token},
                                   {"$set": {"expires_at": now + timedelta(seconds=self.ttl)}})
        if res.matched_count != 1:
            raise LeaseLost(f"LEASE_LOST: {self.key} token {self.token} was taken over by another worker; refusing to write")

    def release(self) -> None:
        if self.token is None:
            return
        try:
            now = self._now()
            self.coll.update_one({"_id": self.key, "owner": self.owner, "token": self.token},
                                 {"$set": {"expires_at": now}})        # expire now; keep the doc so the token stays monotonic
        except Exception as exc:
            logger.error("[LEASE_RELEASE_FAILED] %s: %s (it will expire on its own in <= %.0fs)", self.key, exc, self.ttl)
        finally:
            self.token = None
