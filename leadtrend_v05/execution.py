"""Simulated execution at next-session open: whole shares, fees, adverse slippage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .ledger import AccountState, record_trade_cash


@dataclass
class Trade:
    session_date: date
    security_id: str
    side: str  # buy | sell
    shares: int
    reference_price: float
    fill_price: float
    fee: float
    cash_delta: float
    reason: str


def adverse_fill(reference_price: float, side: str, slippage_rate: float) -> float:
    if side == "buy":
        return reference_price * (1.0 + slippage_rate)
    return reference_price * (1.0 - slippage_rate)


def plan_exit_cash_trades(account, target_ids, open_prices, fee_per_trade,
                          slippage_rate, session_date, is_swap_week, cap=None, marks=None, total_pre=None, **kwargs):
    """R2: exits finance additions; retained positions are not resized unless a single-stock cap is given.

    cap (plan 9.4(2)): in a trading week, a retained name above cap x total equity (total_pre, at the execution
    session's closing marks before any order) is cut to the largest whole-share holding within the cap. The sale
    proceeds join the cash that the additions split. cap=None leaves the trades exactly as before.
    """
    if not is_swap_week:return []
    targets=set(target_ids);held={s for s,q in account.positions.items() if q>0}
    additions=sorted(targets-held);trades=[]
    for sid in sorted(held-targets):
        qty=account.shares_of(sid);ref=open_prices[sid]
        fill=adverse_fill(ref,'sell',slippage_rate)
        delta=record_trade_cash(account,sid,qty,'sell',fill,fee_per_trade)
        trades.append(Trade(session_date,sid,'sell',qty,ref,fill,fee_per_trade,delta,'exit'))
    if cap is not None:
        for sid in sorted(held&targets):
            qty=account.shares_of(sid);keep=min(qty,int(cap*total_pre//marks[sid]))
            if qty-keep<=0:continue
            ref=open_prices[sid];fill=adverse_fill(ref,'sell',slippage_rate)
            delta=record_trade_cash(account,sid,qty-keep,'sell',fill,fee_per_trade)
            trades.append(Trade(session_date,sid,'sell',qty-keep,ref,fill,fee_per_trade,delta,'cap_trim'))
    if not additions:return trades
    budget=max(0.,account.cash-len(additions)*fee_per_trade)/len(additions)
    for sid in additions:
        ref=open_prices[sid];fill=adverse_fill(ref,'buy',slippage_rate)
        qty=min(int(budget//fill),max(0,int((account.cash-fee_per_trade)//fill)))
        if qty<=0:continue
        delta=record_trade_cash(account,sid,qty,'buy',fill,fee_per_trade)
        trades.append(Trade(session_date,sid,'buy',qty,ref,fill,fee_per_trade,delta,'fill_from_exit_cash'))
    return trades


def plan_equal_weight_trades(
    account: AccountState,
    target_ids: list[str],
    open_prices: dict[str, float],
    fee_per_trade: float,
    slippage_rate: float,
    session_date: date,
    is_swap_week: bool,
    reason_prefix: str = "",
    allocation_slots: int | None = None,
) -> list[Trade]:
    """If not a swap week: no orders (weights drift). If swap week: near equal weight.

    Net difference only — never sell and buy the same security in one plan.
    Sells first, then buys. Integer shares. Cash cannot go negative.
    """
    if not is_swap_week:
        return []

    targets = sorted(set(target_ids))
    if not targets:
        # liquidate everything is still a swap; handle exits via empty target
        pass

    for sid in targets:
        if sid not in open_prices:
            raise KeyError(f"missing open price for target {sid}")
        if open_prices[sid] <= 0:
            raise ValueError(f"non-positive open price for {sid}")

    # Positions not in target must be sold completely (exit).
    exits = [
        sid
        for sid in sorted(account.positions.keys())
        if account.shares_of(sid) > 0 and sid not in set(targets)
    ]

    # --- sell pass ---
    trades: list[Trade] = []
    for sid in exits:
        qty = account.shares_of(sid)
        if qty <= 0:
            continue
        ref = open_prices[sid]
        fill = adverse_fill(ref, "sell", slippage_rate)
        fee = fee_per_trade
        cash_delta = record_trade_cash(account, sid, qty, "sell", fill, fee)
        trades.append(
            Trade(
                session_date=session_date,
                security_id=sid,
                side="sell",
                shares=qty,
                reference_price=ref,
                fill_price=fill,
                fee=fee,
                cash_delta=cash_delta,
                reason=f"{reason_prefix}exit",
            )
        )

    if not targets:
        return trades

    # Budget at open prices after exits: cash + remaining target position value
    position_value = 0.0
    for sid in targets:
        qty = account.shares_of(sid)
        position_value += qty * open_prices[sid]
    total_equity_at_open = account.cash + position_value
    n = len(targets) if allocation_slots is None else allocation_slots
    if n < len(targets) or n <= 0:
        raise ValueError('allocation slots smaller than tradable target count')
    budget_each = total_equity_at_open / n

    # Ideal shares using open price as the budgeting quote, reserving the per-trade fee.
    ideal: dict[str, int] = {}
    for sid in targets:
        ref = open_prices[sid]
        if budget_each <= fee_per_trade:
            ideal[sid] = 0
            continue
        q = int((budget_each - fee_per_trade) // ref)
        if q < 0:
            q = 0
        ideal[sid] = q

    # Net differences
    deltas: dict[str, int] = {}
    for sid in targets:
        deltas[sid] = ideal[sid] - account.shares_of(sid)

    # Sells (reductions) first — only net negative, never paired with buy.
    for sid in targets:
        d = deltas[sid]
        if d >= 0:
            continue
        qty = -d
        ref = open_prices[sid]
        fill = adverse_fill(ref, "sell", slippage_rate)
        cash_delta = record_trade_cash(account, sid, qty, "sell", fill, fee_per_trade)
        trades.append(
            Trade(
                session_date=session_date,
                security_id=sid,
                side="sell",
                shares=qty,
                reference_price=ref,
                fill_price=fill,
                fee=fee_per_trade,
                cash_delta=cash_delta,
                reason=f"{reason_prefix}rebalance_reduce",
            )
        )

    # Buys with remaining cash, stable security_id order.
    for sid in targets:
        d = deltas[sid]
        if d <= 0:
            continue
        want = d
        ref = open_prices[sid]
        fill = adverse_fill(ref, "buy", slippage_rate)
        max_affordable = int((account.cash - fee_per_trade) // fill)
        if max_affordable < 0:
            max_affordable = 0
        qty = min(want, max_affordable)
        if qty <= 0:
            continue
        cash_delta = record_trade_cash(account, sid, qty, "buy", fill, fee_per_trade)
        trades.append(
            Trade(
                session_date=session_date,
                security_id=sid,
                side="buy",
                shares=qty,
                reference_price=ref,
                fill_price=fill,
                fee=fee_per_trade,
                cash_delta=cash_delta,
                reason=f"{reason_prefix}rebalance_add",
            )
        )

    return trades
