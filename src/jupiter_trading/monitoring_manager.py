from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Callable, Optional

from .candidate_queue import CandidateQueue, RankedCandidate

STRATEGIES = ("A", "B", "C")


@dataclass
class StrategyMonitoringState:
    strategy: str
    state: str = "WATCHING"
    reason: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "state": self.state,
            "reason": self.reason,
            "updated_at": self.updated_at,
        }


@dataclass
class MonitoringSlot:
    slot_id: int
    instrument_key: Optional[str] = None
    symbol: Optional[str] = None
    candidate_rank: Optional[int] = None
    ranking_version: Optional[int] = None
    assigned_at: Optional[datetime] = None
    lease_expires_at: Optional[datetime] = None
    near_signal: bool = False
    strategies: dict[str, StrategyMonitoringState] = field(
        default_factory=lambda: {
            strategy: StrategyMonitoringState(strategy) for strategy in STRATEGIES
        }
    )

    @property
    def assigned(self) -> bool:
        return self.instrument_key is not None

    @property
    def protected(self) -> bool:
        return self.near_signal or any(
            state.state in {"ARMED", "SIGNALLED", "ENTRY_PENDING", "OPEN", "EXIT_PENDING"}
            for state in self.strategies.values()
        )

    def to_dict(self, now: Optional[datetime] = None) -> dict:
        clock = now or datetime.now(timezone.utc)
        return {
            "slot_id": self.slot_id,
            "instrument_key": self.instrument_key,
            "symbol": self.symbol,
            "candidate_rank": self.candidate_rank,
            "ranking_version": self.ranking_version,
            "assigned_at": self.assigned_at.isoformat() if self.assigned_at else None,
            "lease_expires_at": (
                self.lease_expires_at.isoformat() if self.lease_expires_at else None
            ),
            "lease_remaining_seconds": (
                max(0.0, (self.lease_expires_at - clock).total_seconds())
                if self.lease_expires_at
                else None
            ),
            "near_signal": self.near_signal,
            "protected": self.protected,
            "strategies": {
                key: value.to_dict() for key, value in self.strategies.items()
            },
        }


@dataclass
class Cooldown:
    instrument_key: str
    symbol: str
    started_at: datetime
    expires_at: datetime
    ranking_version: int
    reason: str

    def to_dict(self) -> dict:
        return {
            "instrument_key": self.instrument_key,
            "symbol": self.symbol,
            "started_at": self.started_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "ranking_version": self.ranking_version,
            "reason": self.reason,
        }


class MonitoringManager:
    """Owns exactly N reusable stock leases and deterministic rotations."""

    def __init__(
        self,
        candidate_queue: CandidateQueue,
        slot_count: int = 10,
        lease_seconds: float = 300.0,
        cooldown_seconds: float = 600.0,
        event_sink: Optional[Callable[..., None]] = None,
    ) -> None:
        if slot_count <= 0 or lease_seconds <= 0 or cooldown_seconds < 0:
            raise ValueError("slot, lease, and cooldown settings are invalid")
        self.candidate_queue = candidate_queue
        self.slot_count = slot_count
        self.lease_seconds = lease_seconds
        self.cooldown_seconds = cooldown_seconds
        self._event_sink = event_sink
        self._lock = RLock()
        self._slots = [MonitoringSlot(index + 1) for index in range(slot_count)]
        self._cooldowns: dict[str, Cooldown] = {}
        self._minimum_versions: dict[str, int] = {}
        self._awaiting_requalification: dict[str, int] = {}

    def _emit(self, event_type: str, payload: dict, instrument_key: Optional[str] = None) -> None:
        if self._event_sink:
            self._event_sink(event_type, payload, instrument_key=instrument_key)

    def assigned_keys(self) -> set[str]:
        with self._lock:
            return {slot.instrument_key for slot in self._slots if slot.instrument_key}

    def slot_for(self, instrument_key: str) -> Optional[MonitoringSlot]:
        with self._lock:
            return next(
                (slot for slot in self._slots if slot.instrument_key == instrument_key), None
            )

    def rebalance(self, now: Optional[datetime] = None) -> list[MonitoringSlot]:
        clock = now or datetime.now(timezone.utc)
        with self._lock:
            self._expire_cooldowns(clock)
            current_candidates = {
                item["instrument_key"] for item in self.candidate_queue.snapshot()
            }
            for slot in self._slots:
                if slot.instrument_key:
                    latest = self.candidate_queue.get(slot.instrument_key)
                    if latest:
                        slot.candidate_rank = latest.rank
                        slot.ranking_version = latest.ranking_version
                if not slot.assigned or not slot.lease_expires_at or clock < slot.lease_expires_at:
                    continue
                if slot.protected:
                    self._renew(slot, clock, "PROTECTED_STATE")
                elif slot.instrument_key in current_candidates and slot.near_signal:
                    self._renew(slot, clock, "NEAR_SIGNAL")
                else:
                    self._emit(
                        "LEASE_EXPIRED",
                        {**slot.to_dict(clock), "reason": "LEASE_EXPIRED"},
                        slot.instrument_key,
                    )
                    self._release(slot, clock, "LEASE_EXPIRED")

            excluded = self.assigned_keys() | set(self._cooldowns)
            for slot in self._slots:
                if slot.assigned:
                    continue
                candidate = self.candidate_queue.next(excluded, self._minimum_versions)
                if candidate is None:
                    break
                self._assign(slot, candidate, clock)
                excluded.add(candidate.instrument_key)
            return list(self._slots)

    def rotate(self, slot_id: int, reason: str = "MANUAL_ROTATION", now: Optional[datetime] = None) -> MonitoringSlot:
        clock = now or datetime.now(timezone.utc)
        with self._lock:
            slot = self._slots[slot_id - 1]
            if slot.protected:
                raise ValueError("a slot with an armed setup or open position cannot rotate")
            self._release(slot, clock, reason)
            self.rebalance(clock)
            return slot

    def update_strategy(
        self,
        instrument_key: str,
        strategy: str,
        state: str,
        reason: Optional[str],
        now: Optional[datetime] = None,
    ) -> None:
        if strategy not in STRATEGIES:
            raise ValueError("unknown strategy")
        clock = now or datetime.now(timezone.utc)
        with self._lock:
            slot = self.slot_for(instrument_key)
            if not slot:
                return
            item = slot.strategies[strategy]
            item.state = state
            item.reason = reason
            item.updated_at = clock.isoformat()

    def mark_near_signal(
        self, instrument_key: str, value: bool, now: Optional[datetime] = None
    ) -> None:
        clock = now or datetime.now(timezone.utc)
        with self._lock:
            slot = self.slot_for(instrument_key)
            if not slot:
                return
            changed = slot.near_signal != value
            slot.near_signal = value
            if changed and value:
                self._emit(
                    "NEAR_SIGNAL",
                    {"slot_id": slot.slot_id, "symbol": slot.symbol},
                    instrument_key,
                )
            if value and slot.lease_expires_at and clock >= slot.lease_expires_at:
                self._renew(slot, clock, "NEAR_SIGNAL")

    def start_cooldown(
        self,
        instrument_key: str,
        symbol: str,
        reason: str,
        now: Optional[datetime] = None,
    ) -> None:
        clock = now or datetime.now(timezone.utc)
        with self._lock:
            cooldown = Cooldown(
                instrument_key,
                symbol,
                clock,
                clock + timedelta(seconds=self.cooldown_seconds),
                self.candidate_queue.version,
                reason,
            )
            self._cooldowns[instrument_key] = cooldown
            self._minimum_versions[instrument_key] = self.candidate_queue.version
            self._awaiting_requalification[instrument_key] = self.candidate_queue.version
            self._emit("COOLDOWN_STARTED", cooldown.to_dict(), instrument_key)
            slot = self.slot_for(instrument_key)
            if slot and not slot.protected:
                self._release(slot, clock, "POST_TRADE_COOLDOWN")

    def restore(self, payload: dict) -> None:
        with self._lock:
            by_id = {item["slot_id"]: item for item in payload.get("slots", [])}
            for slot in self._slots:
                saved = by_id.get(slot.slot_id)
                if not saved or not saved.get("instrument_key"):
                    continue
                slot.instrument_key = saved["instrument_key"]
                slot.symbol = saved.get("symbol")
                slot.candidate_rank = saved.get("candidate_rank")
                slot.ranking_version = saved.get("ranking_version")
                slot.assigned_at = _parse(saved.get("assigned_at"))
                slot.lease_expires_at = _parse(saved.get("lease_expires_at"))
                slot.near_signal = bool(saved.get("near_signal"))
                for strategy, state in saved.get("strategies", {}).items():
                    if strategy in slot.strategies:
                        slot.strategies[strategy] = StrategyMonitoringState(
                            strategy,
                            state.get("state", "WATCHING"),
                            state.get("reason"),
                            state.get("updated_at"),
                        )
            for saved in payload.get("cooldowns", []):
                cooldown = Cooldown(
                    saved["instrument_key"],
                    saved.get("symbol", saved["instrument_key"]),
                    _parse(saved["started_at"]),
                    _parse(saved["expires_at"]),
                    int(saved.get("ranking_version", 0)),
                    saved.get("reason", "RESTORED"),
                )
                self._cooldowns[cooldown.instrument_key] = cooldown
                self._minimum_versions[cooldown.instrument_key] = cooldown.ranking_version
                self._awaiting_requalification[cooldown.instrument_key] = (
                    cooldown.ranking_version
                )
            for key, version in payload.get("awaiting_requalification", {}).items():
                self._awaiting_requalification[key] = int(version)
                self._minimum_versions[key] = max(
                    self._minimum_versions.get(key, -1), int(version)
                )

    def snapshot(self, now: Optional[datetime] = None) -> dict:
        clock = now or datetime.now(timezone.utc)
        with self._lock:
            return {
                "slot_count": self.slot_count,
                "lease_seconds": self.lease_seconds,
                "cooldown_seconds": self.cooldown_seconds,
                "slots": [slot.to_dict(clock) for slot in self._slots],
                "cooldowns": [value.to_dict() for value in self._cooldowns.values()],
                "awaiting_requalification": dict(self._awaiting_requalification),
            }

    def _assign(self, slot: MonitoringSlot, candidate: RankedCandidate, now: datetime) -> None:
        slot.instrument_key = candidate.instrument_key
        slot.symbol = candidate.symbol
        slot.candidate_rank = candidate.rank
        slot.ranking_version = candidate.ranking_version
        slot.assigned_at = now
        slot.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
        slot.near_signal = False
        slot.strategies = {
            strategy: StrategyMonitoringState(strategy, updated_at=now.isoformat())
            for strategy in STRATEGIES
        }
        self._emit("SLOT_ASSIGNED", slot.to_dict(now), candidate.instrument_key)
        if candidate.instrument_key in self._awaiting_requalification:
            self._awaiting_requalification.pop(candidate.instrument_key, None)
            self._emit(
                "STOCK_REQUALIFIED",
                {
                    "slot_id": slot.slot_id,
                    "symbol": candidate.symbol,
                    "ranking_version": candidate.ranking_version,
                },
                candidate.instrument_key,
            )

    def _release(self, slot: MonitoringSlot, now: datetime, reason: str) -> None:
        key = slot.instrument_key
        payload = {**slot.to_dict(now), "reason": reason}
        if key:
            self._minimum_versions[key] = max(
                self._minimum_versions.get(key, -1), slot.ranking_version or -1
            )
        self._emit("SLOT_RELEASED", payload, key)
        if reason != "SESSION_END":
            self._emit("SLOT_ROTATED", payload, key)
        replacement = MonitoringSlot(slot.slot_id)
        slot.__dict__.update(replacement.__dict__)

    def _renew(self, slot: MonitoringSlot, now: datetime, reason: str) -> None:
        slot.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
        self._emit(
            "LEASE_RENEWED",
            {**slot.to_dict(now), "reason": reason},
            slot.instrument_key,
        )

    def _expire_cooldowns(self, now: datetime) -> None:
        for key, cooldown in list(self._cooldowns.items()):
            if now < cooldown.expires_at:
                continue
            self._cooldowns.pop(key, None)
            self._emit("COOLDOWN_EXPIRED", cooldown.to_dict(), key)


def _parse(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None
