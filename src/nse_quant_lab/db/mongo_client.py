"""
mongo_client.py -- Centralized MongoDB Connection & Collection Accessor
"""

import os
from typing import Optional
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database


class MongoDatabase:
    """Singleton MongoDB connection wrapper."""
    _instance: Optional["MongoDatabase"] = None

    def __init__(self, uri: Optional[str] = None, db_name: Optional[str] = None):
        self.uri = uri or os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
        self.db_name = db_name or os.environ.get("MONGO_DB", "stock_scanner")
        self.client: MongoClient = MongoClient(self.uri, serverSelectionTimeoutMS=2000)
        self.db: Database = self.client[self.db_name]

    @classmethod
    def get_instance(cls, uri: Optional[str] = None, db_name: Optional[str] = None) -> "MongoDatabase":
        if cls._instance is None:
            cls._instance = cls(uri, db_name)
        return cls._instance

    def ping(self) -> bool:
        """Pings MongoDB server to confirm connectivity."""
        try:
            self.client.admin.command("ping")
            return True
        except Exception:
            return False

    def get_collection(self, name: str) -> Collection:
        """Returns collection with target name."""
        return self.db[name]

    # Standard Application Collections
    @property
    def ohlcv(self) -> Collection:
        coll_name = os.environ.get("MONGO_COLLECTION", "upstox_daily_ohlcv")
        return self.db[coll_name]

    @property
    def orders(self) -> Collection:
        return self.db["orders"]

    @property
    def executions(self) -> Collection:
        return self.db["executions"]

    @property
    def positions(self) -> Collection:
        return self.db["positions"]

    @property
    def trades(self) -> Collection:
        return self.db["trades"]

    @property
    def daily_nav(self) -> Collection:
        return self.db["daily_nav"]

    @property
    def strategies(self) -> Collection:
        return self.db["strategies"]

    @property
    def backtest_runs(self) -> Collection:
        return self.db["backtest_runs"]
