import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.collection import Collection

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if isinstance(value, str):
        return value.strip().strip("'\" ")
    return value


MONGO_URI = _env("MONGO_CONNECTION") or _env("MONGO_URI")
DB_NAME = _env("DB_NAME") or "TestRunner"
COLLECTION_NAME = _env("TEST_COLLECTION") or "test_run_results"
TEST_CONFIG_COLLECTION_NAME = _env("TEST_CONFIG_COLLECTION") or "test_config"

client: Optional[MongoClient]
if MONGO_URI:
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    except Exception as exc:
        logger.warning(f"MongoDB client init failed, 'db' storage will be unavailable: {exc}")
        client = None
else:
    client = None

if client:
    db = client[DB_NAME]
    collection: Optional[Collection] = db[COLLECTION_NAME]
    test_config_collection: Optional[Collection] = db[TEST_CONFIG_COLLECTION_NAME]
else:
    db = None
    collection = None
    test_config_collection = None


def connect_mongo() -> None:
    if client is None or collection is None or test_config_collection is None:
        raise RuntimeError("MongoDB connection is not configured. Set MONGO_CONNECTION in .env.")
    try:
        client.admin.command("ping")
    except Exception as exc:
        raise RuntimeError(f"Unable to connect to MongoDB at {MONGO_URI}: {exc}") from exc


def close_mongo() -> None:
    if client is not None:
        client.close()
