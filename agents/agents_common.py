"""
Small shared helpers used by every agent module in this package.

Not itself an agent — just avoids duplicating the same JSONL-logging and
JSON-fence-stripping snippets in agents_monitor.py / agents_orchestrator.py /
agents_analysis.py / agents_incident.py.
"""

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("agents.common")

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)


def strip_json_fences(text: str) -> str:
    """Models can wrap JSON in fences even when told not to; strip before parsing."""
    return text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()


def log_jsonl(name: str, entry: dict) -> None:
    """Append one entry to logs/<name>_<date>.jsonl, so every agent's calls
    are inspectable after the fact. `name` is the agent ('monitor',
    'orchestrator', 'analysis', 'incident')."""
    log_file = LOG_DIR / f"{name}_{time.strftime('%Y%m%d')}.jsonl"
    try:
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), **entry}, default=str) + "\n")
    except Exception as e:
        logger.warning(f"Could not write {name} call log: {e}")
