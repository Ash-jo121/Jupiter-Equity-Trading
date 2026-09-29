from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Callable, Optional

from .research_store import ResearchStore


@dataclass
class ExecutionIntent:
    id: str
    session_id: str
    strategy: str
    account_id: str
    instrument_key: str
    symbol: str
    side: str
    quantity: int
    observed_price: float
    reason: str
    created_at: str
    status: str = "CREATED"
    metadata: dict = field(default_factory=dict)
    order: Optional[dict] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class ExecutionQueue:
    """Bounded, persistent, idempotent in-process execution intent queue."""

    def __init__(
        self,
        store: ResearchStore,
        session_id: str,
        executor: Callable[[ExecutionIntent], dict],
        event_sink: Optional[Callable[..., None]] = None,
        max_pending: int = 100,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.executor = executor
        self.event_sink = event_sink
        self.max_pending = max_pending
        self._queue: deque[ExecutionIntent] = deque()
        self._lock = RLock()
        for payload in reversed(store.execution_intents(session_id, status="QUEUED", limit=max_pending)):
            self._queue.append(ExecutionIntent(**payload))

    def enqueue(self, intent: ExecutionIntent) -> bool:
        with self._lock:
            if intent.session_id != self.session_id:
                raise ValueError("intent belongs to another monitoring session")
            if self.store.execution_intent(intent.id):
                return False
            if len(self._queue) >= self.max_pending:
                raise RuntimeError("execution intent queue is full")
            intent.status = "QUEUED"
            if not self.store.save_execution_intent(intent.to_dict()):
                return False
            self._queue.append(intent)
            self._emit("EXECUTION_INTENT_CREATED", intent)
            return True

    def drain(self, limit: int = 20) -> list[ExecutionIntent]:
        processed = []
        for _ in range(max(0, limit)):
            with self._lock:
                if not self._queue:
                    break
                intent = self._queue.popleft()
            # Persist ACCEPTED before touching the broker. A restart therefore
            # cannot re-submit an intent whose outcome write was interrupted.
            intent.status = "ACCEPTED"
            self.store.save_execution_intent(intent.to_dict())
            self._emit("EXECUTION_INTENT_ACCEPTED", intent)
            try:
                order = self.executor(intent)
                intent.order = order
                filled = int(order.get("filled_quantity", 0))
                intent.status = (
                    "FILLED"
                    if filled >= intent.quantity
                    else "PARTIAL"
                    if filled > 0
                    else "REJECTED"
                )
                intent.error = order.get("rejection_reason")
            except Exception as error:  # noqa: BLE001 - isolate one execution
                intent.status = "REJECTED"
                intent.error = str(error)[:500]
            self.store.save_execution_intent(intent.to_dict())
            self._emit(
                "ENTRY_FILLED"
                if intent.side == "BUY" and intent.status in {"FILLED", "PARTIAL"}
                else "EXIT_FILLED"
                if intent.side == "SELL" and intent.status in {"FILLED", "PARTIAL"}
                else "EXECUTION_INTENT_REJECTED",
                intent,
            )
            processed.append(intent)
        return processed

    def snapshot(self) -> dict:
        with self._lock:
            return {"pending": len(self._queue), "capacity": self.max_pending}

    def _emit(self, event_type: str, intent: ExecutionIntent) -> None:
        if self.event_sink:
            self.event_sink(
                event_type,
                {"intent": intent.to_dict()},
                instrument_key=intent.instrument_key,
                strategy=intent.strategy,
            )


def intent_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
