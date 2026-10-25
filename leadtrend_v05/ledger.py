"""Account ledger: shares, cash, receivable cash; corporate actions; equity identity."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Position:
    security_id: str
    shares: int
    # cash dividends entitlement uses this historical holding; not used for voting etc.


@dataclass
class Receivable:
    event_id: str
    security_id: str
    amount: float
    ex_date: date
    pay_date: date | None
    entitled_shares: int


@dataclass
class AccountState:
    cash: float
    positions: dict[str, int] = field(default_factory=dict)
    receivable_cash: float = 0.0
    receivables: list[Receivable] = field(default_factory=list)
    processed_event_ids: set[str] = field(default_factory=set)

    def shares_of(self, security_id: str) -> int:
        return int(self.positions.get(security_id, 0))

    def set_shares(self, security_id: str, shares: int) -> None:
        if shares < 0:
            raise ValueError(f"negative shares for {security_id}")
        if shares == 0:
            self.positions.pop(security_id, None)
        else:
            self.positions[security_id] = int(shares)

    def equity_components(self, mark: dict[str, float]) -> tuple[float, float, float, float]:
        stock_value = 0.0
        for sid, q in self.positions.items():
            if sid not in mark:
                raise KeyError(f"missing mark price for held {sid}")
            stock_value += q * float(mark[sid])
        total = stock_value + self.cash + self.receivable_cash
        return stock_value, self.cash, self.receivable_cash, total


def apply_split(
    account: AccountState,
    security_id: str,
    shares_new_per_old: float,
    event_id: str,
) -> None:
    if event_id in account.processed_event_ids:
        return
    if shares_new_per_old <= 0:
        raise ValueError(f"invalid shares_new_per_old for {event_id}")
    old = account.shares_of(security_id)
    if old <= 0:
        account.processed_event_ids.add(event_id)
        return
    new_shares = old * shares_new_per_old
    # Stage-1 integer account: synthetic fixtures use ratios that keep integers exact
    # when starting from integer shares only if ratio is integer or .5 etc.
    # We track fractional share equivalent in a precise way via float then round half away
    # from zero only if non-integer would occur — but research account must be exact.
    # Use exact float when ratio * old is integral; otherwise keep float and reject later sells.
    if abs(new_shares - round(new_shares)) < 1e-9:
        account.set_shares(security_id, int(round(new_shares)))
    else:
        # Non-integer result is not allowed for stage-1 whole-share account.
        raise ValueError(
            f"split {event_id} produces non-integer shares: {old} * {shares_new_per_old}"
        )
    account.processed_event_ids.add(event_id)


def confirm_dividend(
    account: AccountState,
    security_id: str,
    event_id: str,
    cash_per_share: float,
    ex_date: date,
    pay_date: date | None,
    entitled_shares: int,
) -> None:
    """Entitlement is shares held at close of session BEFORE ex_date.

    Call this BEFORE open trades on the ex-date so an open sell still receives
    the dividend and an open buy does not.
    """
    if event_id in account.processed_event_ids:
        return
    if entitled_shares <= 0:
        account.processed_event_ids.add(event_id)
        return
    amount = entitled_shares * cash_per_share
    account.receivable_cash += amount
    account.receivables.append(
        Receivable(
            event_id=event_id,
            security_id=security_id,
            amount=amount,
            ex_date=ex_date,
            pay_date=pay_date,
            entitled_shares=entitled_shares,
        )
    )
    account.processed_event_ids.add(event_id)


def pay_receivables(account: AccountState, on_date: date) -> list[Receivable]:
    """Move due receivables to available cash on pay_date."""
    paid: list[Receivable] = []
    remaining: list[Receivable] = []
    for r in account.receivables:
        if r.pay_date is not None and r.pay_date <= on_date:
            account.cash += r.amount
            account.receivable_cash -= r.amount
            paid.append(r)
        else:
            remaining.append(r)
    account.receivables = remaining
    # guard against float drift
    if account.receivable_cash < 0 and account.receivable_cash > -1e-6:
        account.receivable_cash = 0.0
    return paid


def record_trade_cash(
    account: AccountState,
    security_id: str,
    qty: int,
    side: str,
    fill_price: float,
    fee: float,
) -> float:
    """Apply one buy/sell to cash and shares. Returns cash delta."""
    if qty == 0:
        return 0.0
    if qty < 0:
        raise ValueError("qty must be positive; side encodes direction")
    if side == "sell":
        delta = qty * fill_price - fee
        account.set_shares(security_id, account.shares_of(security_id) - qty)
        account.cash += delta
        return delta
    if side == "buy":
        delta = -(qty * fill_price + fee)
        account.set_shares(security_id, account.shares_of(security_id) + qty)
        account.cash += delta
        if account.cash < -1e-6:
            raise ValueError(f"cash cannot be negative: {account.cash}")
        return delta
    raise ValueError(f"unknown side {side}")
