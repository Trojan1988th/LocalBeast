r"""Failure counters (W0.4): nothing gets to fail silently.

Any cron/tool/outbound failure calls bump(name); the vitals check (W0.5) and
the watchdog read the counters. Deliberately tiny: a JSON file of
{name: {count, last_at, last_detail}} — enough to make silence impossible,
not a metrics platform.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

COUNTERS_PATH = Path(__file__).resolve().parents[2] / "data" / "failure_counters.json"
_lock = threading.Lock()


def _load() -> dict:
    if COUNTERS_PATH.exists():
        try:
            return json.loads(COUNTERS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def bump(name: str, detail: str = "") -> None:
    with _lock:
        data = _load()
        entry = data.get(name) or {"count": 0}
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last_at"] = time.time()
        if detail:
            entry["last_detail"] = str(detail)[:300]
        data[name] = entry
        COUNTERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        COUNTERS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def snapshot() -> dict:
    with _lock:
        return _load()


def since(hours: float) -> dict:
    """Counters whose last failure is within the window — 'what's hurting now'."""
    cutoff = time.time() - hours * 3600
    return {k: v for k, v in snapshot().items() if v.get("last_at", 0) >= cutoff}
