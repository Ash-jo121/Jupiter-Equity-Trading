from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from typing import Dict, List, Optional

from .domain import PaperAccount, Quote
from .paper_broker import FeeSchedule, PaperBroker, RiskLimits
from .repository import TradingRepository


class PaperAccountManager:
    """Owns independent persistent paper brokers that share incoming market data."""

    def __init__(
        self,
        repository: TradingRepository,
        initial_cash: float,
        slippage_bps: float,
        fee_schedule: FeeSchedule,
        risk_limits: RiskLimits,
    ) -> None:
        self.repository = repository
        self.initial_cash = initial_cash
        self.slippage_bps = slippage_bps
        self.fee_schedule = fee_schedule
        self.risk_limits = risk_limits
        self._brokers: Dict[str, PaperBroker] = {}
        self._quotes: Dict[str, Quote] = {}
        self._market_statuses: Dict[str, str] = {}
        self._lock = RLock()
        self.create("default", "Default paper account", initial_cash)
        for account in self.repository.list_accounts():
            if account.id not in self._brokers:
                self._brokers[account.id] = self._build(account)

    def list(self) -> List[dict]:
        with self._lock:
            return [broker.snapshot() for broker in self._brokers.values()]

    def get(self, account_id: str = "default") -> PaperBroker:
        with self._lock:
            try:
                return self._brokers[account_id]
            except KeyError as error:
                raise KeyError("paper account not found") from error

    def create(self, account_id: str, name: str, initial_cash: Optional[float] = None) -> PaperBroker:
        with self._lock:
            if account_id in self._brokers:
                raise ValueError("paper account already exists")
            account = PaperAccount(account_id, name, initial_cash or self.initial_cash)
            account = self.repository.ensure_account(account)
            broker = self._build(account)
            self._brokers[account.id] = broker
            return broker

    def reset(self, account_id: str, initial_cash: Optional[float] = None) -> PaperBroker:
        with self._lock:
            current = self.get(account_id).account
            now = datetime.now(timezone.utc)
            account = PaperAccount(
                id=current.id,
                name=current.name,
                initial_cash=initial_cash or current.initial_cash,
                created_at=current.created_at,
                updated_at=now,
                reset_at=now,
            )
            self.repository.reset_account(account)
            broker = self._build(account)
            self._brokers[account.id] = broker
            return broker

    def on_quote(self, quote: Quote) -> list:
        with self._lock:
            self._quotes[quote.instrument_key] = quote
            fills = []
            for broker in self._brokers.values():
                fills.extend(broker.on_quote(quote))
            return fills

    def update_market_status(self, statuses: dict) -> None:
        with self._lock:
            self._market_statuses.update(statuses)
            for broker in self._brokers.values():
                broker.update_market_status(statuses)

    def _build(self, account: PaperAccount) -> PaperBroker:
        broker = PaperBroker(
            initial_cash=account.initial_cash,
            slippage_bps=self.slippage_bps,
            fee_schedule=self.fee_schedule,
            risk_limits=self.risk_limits,
            repository=self.repository,
            account_id=account.id,
            account_name=account.name,
        )
        if self._market_statuses:
            broker.update_market_status(self._market_statuses)
        for quote in self._quotes.values():
            broker.quotes[quote.instrument_key] = quote
        return broker
