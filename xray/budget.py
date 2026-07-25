"""AudD spend guard: a persisted monthly call counter next to the store.

The music pass asks for headroom before identifying cues and records what it
actually spent. Ceiling 0 = unlimited.

The cap is OURS, not AudD's. AudD includes 300 free requests once, at signup,
then bills $5/1,000; there is no recurring free tier and no monthly minimum.
The default below matches that one-time allowance only so a fresh install
cannot spend money by accident. Every call past it is billed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

FILENAME = "audd_budget.json"
DEFAULT_MONTHLY = 300  # = AudD's one-time signup allowance; see module note


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


class AuddBudget:
    def __init__(self, store_dir: Path, monthly: int = DEFAULT_MONTHLY):
        self.path = store_dir / FILENAME
        self.monthly = monthly
        data = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                data = {}
        if data.get("month") != _month():
            data = {"month": _month(), "used": 0}
        self.data = data

    @property
    def used(self) -> int:
        return int(self.data.get("used", 0))

    def headroom(self) -> int | None:
        """Calls left this month, or None when unlimited."""
        if self.monthly <= 0:
            return None
        return max(0, self.monthly - self.used)

    def spend(self, calls: int) -> None:
        self.data["used"] = self.used + calls
        self.path.write_text(json.dumps(self.data))
