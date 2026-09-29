from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Iterable, Optional


@dataclass(frozen=True)
class RankedCandidate:
    instrument_key: str
    symbol: str
    rank: int
    score: float
    ranking_version: int
    updated_at: datetime
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            **self.payload,
            "instrument_key": self.instrument_key,
            "symbol": self.symbol,
            "rank": self.rank,
            "momentum_score": self.score,
            "ranking_version": self.ranking_version,
            "updated_at": self.updated_at.isoformat(),
        }


class CandidateQueue:
    """Latest ranked universe snapshot used by reusable monitoring slots.

    It is intentionally a snapshot rather than an ever-growing queue: every
    five-minute survey replaces stale ranks and bounded memory follows from the
    configured candidate limit.
    """

    def __init__(self, max_candidates: int = 40) -> None:
        if max_candidates < 10:
            raise ValueError("candidate queue must be able to feed ten slots")
        self.max_candidates = max_candidates
        self._lock = RLock()
        self._version = 0
        self._updated_at: Optional[datetime] = None
        self._candidates: list[RankedCandidate] = []

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    @property
    def updated_at(self) -> Optional[datetime]:
        with self._lock:
            return self._updated_at

    def refresh(self, rows: Iterable[dict], at: Optional[datetime] = None) -> list[RankedCandidate]:
        timestamp = at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        ordered = sorted(
            rows,
            key=lambda row: (-float(row.get("momentum_score", 0)), row["instrument_key"]),
        )[: self.max_candidates]
        with self._lock:
            self._version += 1
            self._updated_at = timestamp
            self._candidates = [
                RankedCandidate(
                    instrument_key=row["instrument_key"],
                    symbol=row.get("symbol", row["instrument_key"]),
                    rank=index,
                    score=float(row.get("momentum_score", 0)),
                    ranking_version=self._version,
                    updated_at=timestamp,
                    payload=dict(row),
                )
                for index, row in enumerate(ordered, 1)
            ]
            return list(self._candidates)

    def get(self, instrument_key: str) -> Optional[RankedCandidate]:
        with self._lock:
            return next(
                (item for item in self._candidates if item.instrument_key == instrument_key),
                None,
            )

    def next(
        self,
        excluded: set[str],
        minimum_versions: Optional[dict[str, int]] = None,
    ) -> Optional[RankedCandidate]:
        minimum_versions = minimum_versions or {}
        with self._lock:
            return next(
                (
                    item
                    for item in self._candidates
                    if item.instrument_key not in excluded
                    and item.ranking_version > minimum_versions.get(item.instrument_key, -1)
                ),
                None,
            )

    def snapshot(self, limit: Optional[int] = None) -> list[dict]:
        with self._lock:
            candidates = self._candidates[:limit] if limit else self._candidates
            return [item.to_dict() for item in candidates]

