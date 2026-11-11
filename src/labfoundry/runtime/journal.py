"""Durable structured runtime journal for scenario lifecycle diagnostics."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class RunJournal:
    """Append JSON Lines with per-instance sequence numbers and fsync after each record."""

    def __init__(self, run_dir: Path) -> None:
        self.path = run_dir / "internal" / "run.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sequence = 0
        self._lock = threading.Lock()

    def record(self, event: str, **details: Any) -> None:
        """Append caller-supplied details to a lifecycle record and flush it to stable storage."""
        with self._lock:
            self._sequence += 1
            payload = {
                **details,
                "sequence": self._sequence,
                "timestamp": datetime.now(UTC).isoformat(),
                "event": event,
            }
            encoded = (
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                + "\n"
            )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
