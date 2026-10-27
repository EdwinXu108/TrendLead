"""Backtest engine v1 on the Top 200 database. Reads only through leadtrend_db.api.

Execution model (investor rules of 2026-10-27):
  * signals are computed after the close of the last session of each ISO week;
  * orders are filled at the next session's opening price, buys at open*(1+slip), sells at open*(1-slip);
  * 1 USD per order, whole shares (fractional only for the weight portfolios and the benchmark), cash never negative,
    sells before buys;
  * R2: a kept position is never resized; the sale proceeds plus existing cash are split equally among the new buys;
  * cash earns nothing; the benchmark is VOO held as total-return units from the first trade's session.
Tranche K splits the capital into K sub-accounts; sub-account j starts on signal week j and then checks every K weeks.
The engine refuses an end date after 2024-06-28 (the reserved segment is not to be used).
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field

from leadtrend_db import api

LAST_ALLOWED_END = '2024-06-28'
BENCH = 'BENCH:VOO'
POOL_MODES = ('pool_ew', 'pool_cw', 'top10_cw', 'pool')
_MARKET_CACHE: dict[tuple, dict] = {}      # read-only market data shared by runs with the same window


class HoldoutError(ValueError):
    """The requested window touches the reserved segment."""


def _hash_key(seed, day: str, sid: str) -> str:
    return hashlib.sha256(f'{seed}|{day}|{sid}'.encode()).hexdigest()


def _percentiles(values: list[float]) -> list[float]:
    """Average-rank percentile in [0, 1] (0 = smallest, 1 = largest; ties share the average rank)."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        rank = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = rank
        i = j + 1
    return [r / (n - 1) for r in ranks]


@dataclass
class Tranche:
    index: int
    cash: float
    positions: dict[str, float] = field(default_factory=dict)
    cost: dict[str, float] = field(default_factory=dict)          # open cost basis per security
    issuer_of: dict[str, str] = field(default_factory=dict)
    buy_cost: dict[str, float] = field(default_factory=dict)      # per issuer
    sell_proceeds: dict[str, float] = field(default_factory=dict)
    income: dict[str, float] = field(default_factory=dict)
    trades: int = 0


class Backtest:
    def __init__(self, cfg: dict, db_path=None, market: dict | None = None):
        self.cfg = dict(cfg)
        self.market = market
        self.start = self.cfg['start']
        self.end = self.cfg['end']
        if self.end > LAST_ALLOWED_END:
            raise HoldoutError(f'end {self.end} is after {LAST_ALLOWED_END}: the reserved segment must not be used')
        if self.start >= self.end:
            raise ValueError('start must be before end')
        self.mode = self.cfg.get('mode', 'momentum')
        self.L = int(self.cfg.get('L', 90))
        self.N = int(self.cfg.get('N', 10))
        self.b = float(self.cfg.get('b', 2))
        self.K = int(self.cfg.get('K', 1))
        self.top_n = int(self.cfg['top_n']) if self.cfg.get('top_n') else None
        self.weighting = str(self.cfg.get('weighting', 'cw'))
        self.rebalance = str(self.cfg.get('rebalance', 'R2'))
        self.top_m = int(self.cfg['top_m']) if self.cfg.get('top_m') else None
        self.w = float(self.cfg['w']) if self.cfg.get('w') is not None else None
        self.buy_weighting = str(self.cfg.get('buy_weighting', 'equal'))
        self.buy_plan = self.cfg.get('buy_plan') or {}
        self.size_fallback = 0
        self.slip = float(self.cfg.get('cost_bps', 0)) / 10000.0
        self.fee = float(self.cfg.get('fee_per_order', 1.0))
        self.capital = float(self.cfg.get('capital', 100000.0))
        self.fractional = bool(self.cfg.get('fractional', self.mode in POOL_MODES or self.mode == 'voo'))
        self.db_path = db_path
        self.seed = self.cfg.get('seed')
        self.plan = self.cfg.get('plan') or {}
        self.calendar: list[str] = []
        self.signal_days: list[str] = []
        self.tri: dict[str, dict[str, float]] = {}
        self.opens: dict[str, dict[str, float]] = {}
        self.closes: dict[str, dict[str, float]] = {}
        self.last_close: dict[str, float] = {}
        self.actions: dict[str, list[dict]] = {}
        self.action_sids: list[str] = []
        self.spin_value: dict[tuple[str, str], float] = {}
        self.pools: dict[str, dict] = {}
        self.pool_days: dict[str, str] = {}
        self.amounts: dict[str, dict[str, float]] = {}
        self.issuer: dict[str, str] = {}
        self.tranches: list[Tranche] = []
        self.decisions: list[dict] = []
        self.trades: list[dict] = []
        self.daily: list[dict] = []
        self.position_rows: list[dict] = []
        self.positions_today: list[dict] = []
        self.pending: list[dict] = []
        self.applied: dict[str, int] = defaultdict(int)
        self.bench_units = 0.0
        self.bench_entry = None
        self.first_trade_day = None

    # -- data ---------------------------------------------------------------------------------------------------
    def _cache_key(self):
        family = 'pool' if self.mode in POOL_MODES else ('voo' if self.mode == 'voo' else 'signal')
        return (str(self.db_path), self.start, self.end, family)

    def load(self) -> None:
        if self.market is not None:
            self._load()
            return
        key = self._cache_key()
        cached = _MARKET_CACHE.get(key)
        if cached is not None:
            self.calendar = cached['calendar']
            self.full_calendar = cached['full_calendar']
            self.signal_days = cached['signal_days']
            self.pools = cached['pools']
            self.pool_days = cached['pool_days']
            self.tri = cached['tri']
            self.opens = cached['opens']
            self.closes = cached['closes']
            self.issuer = cached['issuer']
            self.amounts = cached['amounts']
            self.actions = cached['actions']
            self.action_sids = list(cached['action_sids'])
            self.spin_value = cached['spin_value']
            return
        self._load()
        _MARKET_CACHE[key] = dict(calendar=self.calendar, full_calendar=self.full_calendar,
                                  signal_days=self.signal_days, pools=self.pools, pool_days=self.pool_days,
                                  tri=self.tri, opens=self.opens, closes=self.closes, issuer=self.issuer,
                                  amounts=self.amounts,
                                  actions=self.actions, action_sids=list(self.action_sids),
                                  spin_value=self.spin_value)

    def _load(self) -> None:
        if self.market is not None:
            self.calendar = list(self.market['calendar'])
            self.full_calendar = list(self.market.get('full_calendar', self.market['calendar']))
            self.signal_days = [d for d in self.market['signal_days'] if d >= self.start]
            self.pools = self.market['pools']
            self.pool_days = self.market.get('pool_days', {})
            self.tri = self.market['tri']
            self.opens = self.market['opens']
            self.closes = self.market['closes']
            self.issuer = self.market.get('issuer', {})
            self.actions = self.market.get('actions', {})
            self.amounts = {}
            for day, info in self.pools.items():
                if 'amounts' in info:
                    self.amounts[day] = {sid: float(row['amount_cents']) for sid, row in info['amounts'].items()}
                else:
                    self.amounts[day] = {sec['security_id']: float(sec.get('amount_cents', 0.0))
                                         for sec in info.get('securities', [])}
            self._prepare_actions()
            return
        db = self.db_path
        from datetime import date, timedelta
        lookback = (date.fromisoformat(self.start) - timedelta(days=400)).isoformat()
        self.full_calendar = api.trading_days(lookback, self.end, db)
        self.calendar = [d for d in self.full_calendar if d >= self.start]
        self.signal_days = [d for d in api.friday_signal_days(self.start, self.end, db) if d >= self.start]
        if not self.signal_days:
            raise ValueError('no signal day in the window')
        ids: set[str] = set()
        if self.mode == 'voo':
            ids = {BENCH}
        elif self.mode in POOL_MODES:
            windows = [w for w in api.pool_windows(self.start, self.end, db) if w['effective_to'] >= self.start]
            for window in windows:
                amounts = api.pool_amounts(window['version_id'], db)
                day = max(window['effective_from'], self.start)
                if day > self.end:
                    continue
                day = next((d for d in self.calendar if d >= day), None)
                if day is None:
                    continue
                self.pools[day] = dict(version_id=window['version_id'], amounts=amounts)
                self.pool_days[day] = window['version_id']
                ids.update(amounts)
                for sid, row in amounts.items():
                    self.issuer[sid] = row['issuer_id']
        else:
            amount_cache: dict[str, dict] = {}
            for day in self.signal_days:
                pool = api.pool_at(day, db)
                version = pool['version_id']
                if version not in amount_cache:
                    amount_cache[version] = api.pool_amounts(version, db) if version else {}
                amounts = amount_cache[version]
                self.pools[day] = pool
                self.amounts[day] = {}
                ids.update(s['security_id'] for s in pool['securities'])
                for sec in pool['securities']:
                    sid = sec['security_id']
                    self.issuer[sid] = sec['issuer_id']
                    row = amounts.get(sid)
                    amount = float(row['amount_cents']) if row else 0.0
                    sec['amount_cents'] = amount
                    self.amounts[day][sid] = amount
        ids.add(BENCH)
        series = api.tri_series(sorted(ids), self.full_calendar[0], self.end, db)
        for sid, rows in series.items():
            self.tri[sid] = {r[0]: r[1] for r in rows}
        self.opens = api.open_series(sorted(ids), self.start, self.end, db)
        self._load_closes(sorted(ids), db)
        self._load_actions(sorted(ids), db)
        bars = api.benchmark_bars(self.full_calendar[0], self.end)     # VOO is not in prices_daily
        self.opens[BENCH] = {d: b['open'] for d, b in bars.items()}
        self.closes[BENCH] = {d: b['close'] for d, b in bars.items()}
        self._prepare_actions()

    def _load_closes(self, ids: list[str], db) -> None:
        conn = api.connect(db) if db else api.connect()
        for sid in ids:
            rows = conn.execute("""select session_date, close from prices_daily where security_id=? and source='opend_none'
                                   and session_date between ? and ? order by session_date""",
                                (sid, self.start, self.end))
            self.closes[sid] = {r[0]: float(r[1]) for r in rows}

    def _load_actions(self, ids: list[str], db) -> None:
        conn = api.connect(db) if db else api.connect()
        for sid in ids:
            rows = [dict(r) for r in conn.execute("""
                select action_date, action_type, ratio, cash_amount, spin_value, vendor_factor
                from corporate_actions where security_id=? and action_date between ? and ? order by action_date""",
                (sid, self.start, self.end))]
            self.actions[sid] = rows
            for row in rows:
                if row['action_type'] != 'spinoff':
                    continue
                if row['spin_value'] is not None:
                    self.spin_value[(sid, row['action_date'])] = float(row['spin_value'])
                    continue
                prior = [d for d in self.closes.get(sid, {}) if d < row['action_date']]
                prev_close = self.closes[sid][max(prior)] if prior else None
                if prev_close is None:
                    continue
                if row['vendor_factor'] is not None:
                    self.spin_value[(sid, row['action_date'])] = (1 - float(row['vendor_factor'])) * prev_close
                elif row['action_date'] in self.opens.get(sid, {}):
                    self.spin_value[(sid, row['action_date'])] = max(
                        0.0, prev_close - self.opens[sid][row['action_date']])

    def _prepare_actions(self) -> None:
        self.action_sids = [sid for sid in sorted(self.actions) if self.actions[sid]]
        for sid in self.action_sids:
            for row in self.actions[sid]:
                if row['action_type'] != 'spinoff':
                    continue
                if row.get('spin_value') is not None:
                    self.spin_value[(sid, row['action_date'])] = float(row['spin_value'])
                    continue
                prior = [d for d in self.closes.get(sid, {}) if d < row['action_date']]
                prev_close = self.closes[sid][max(prior)] if prior else None
                if prev_close is None:
                    continue
                if row.get('vendor_factor') is not None:
                    self.spin_value[(sid, row['action_date'])] = (1 - float(row['vendor_factor'])) * prev_close
                elif row['action_date'] in self.opens.get(sid, {}):
                    self.spin_value[(sid, row['action_date'])] = max(
                        0.0, prev_close - self.opens[sid][row['action_date']])

    def close_on(self, sid: str, day: str) -> float | None:
        value = self.closes.get(sid, {}).get(day)
        return value if value is not None else self.last_close.get(sid)

    # -- signals ------------------------------------------------------------------------------------------------
    def _in_range(self, pool: dict) -> list[dict]:
        """The scoreable universe: all securities, or the top_m companies by fund amount (largest class per company)."""
        if not self.top_m:
            return list(pool['securities'])
        best: dict[str, tuple[float, str]] = {}
        for sec in pool['securities']:
            issuer = sec['issuer_id']
            amount = float(sec.get('amount_cents', 0.0))
            if issuer not in best or amount > best[issuer][0]:
                best[issuer] = (amount, issuer)
        ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))[:self.top_m]
        keep = {issuer for issuer, _ in ranked}
        return [sec for sec in pool['securities'] if sec['issuer_id'] in keep]

    def _size_groups(self, securities: list[dict]) -> list[list[str]]:
        """Five equal-count size groups by fund amount (0 = largest), one company per issuer."""
        best: dict[str, float] = {}
        for sec in securities:
            issuer = sec['issuer_id']
            amount = float(sec.get('amount_cents', 0.0))
            if issuer not in best or amount > best[issuer]:
                best[issuer] = amount
        ranked = sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))
        n = len(ranked)
        group_of_issuer = {issuer: min(4, i * 5 // n) for i, (issuer, _) in enumerate(ranked)} if n else {}
        groups: list[list[str]] = [[] for _ in range(5)]
        for sec in securities:
            group = group_of_issuer.get(sec['issuer_id'])
            if group is not None:
                groups[group].append(sec['security_id'])
        return groups

    def _scored(self, day: str, pool: dict) -> list[tuple[str, float]]:
        i = bisect.bisect_left(self.full_calendar, day)
        if i < self.L:
            return []
        base_day = self.full_calendar[i - self.L]
        rows = []
        for sec in self._in_range(pool):
            sid = sec['security_id']
            series = self.tri.get(sid)
            if not series or day not in series or base_day not in series or series[base_day] <= 0:
                continue
            rows.append((sid, series[day] / series[base_day] - 1.0, float(sec.get('amount_cents', 0.0))))
        if self.w is None or self.w == 0:
            out = [(sid, mom) for sid, mom, _ in rows]
        else:
            p_mom = _percentiles([r[1] for r in rows])
            p_size = _percentiles([r[2] for r in rows])
            out = [(rows[k][0], self.w * p_size[k] + (1.0 - self.w) * p_mom[k]) for k in range(len(rows))]
        out.sort(key=lambda item: (-item[1], item[0]))
        return out

    def _momentum_orders(self, tranche: Tranche, day: str, pool: dict) -> dict:
        ranked = self._scored(day, pool)
        rank = {sid: n + 1 for n, (sid, _) in enumerate(ranked)}
        in_pool = {s['security_id'] for s in self._in_range(pool)}
        held = list(tranche.positions)
        sells, keeps = [], []
        for sid in held:
            if sid not in in_pool or sid not in rank or rank[sid] > self.b * self.N:
                sells.append(sid)
            else:
                keeps.append(sid)
        limit = min(self.N, len(ranked))
        used_issuer = {self.issuer.get(sid, '') for sid in keeps}
        buys = []
        for sid, _ in ranked:
            if len(keeps) + len(buys) >= limit:
                break
            if sid in tranche.positions:
                continue
            issuer = self.issuer.get(sid, '')
            if issuer and issuer in used_issuer:
                continue
            buys.append(sid)
            used_issuer.add(issuer)
        scores = dict(ranked)
        return dict(sells=sells, buys=buys, rank=rank, keeps=keeps, scores=scores)

    def _random_orders(self, tranche: Tranche, day: str, pool: dict) -> dict:
        """Random control (amendment 1).

        Forced exits of holdings that left the range come first. If the original configuration decided more sells
        than that on this signal day, the difference is drawn from the remaining holdings in hash order. Then the
        book is filled back to N in the same hash order, skipping held names and duplicate issuers.

        In `random_size` mode each buy slot takes the size quintile of the original configuration's buy of the same
        slot on that day; an empty group borrows from the nearest non-empty group and counts a fallback.
        """
        range_secs = self._in_range(pool)
        in_pool = {s['security_id'] for s in range_secs}
        held = sorted(tranche.positions)
        forced = [sid for sid in held if sid not in in_pool]
        sells = list(forced)
        extra = int(self.plan.get(f'{day}|{tranche.index}', 0)) - len(forced)
        if extra > 0:
            rest = [sid for sid in held if sid in in_pool]
            sells += sorted(rest, key=lambda sid: _hash_key(self.seed, day, sid))[:extra]
        kept = [sid for sid in held if sid not in sells]
        used_issuer = {self.issuer.get(sid, '') for sid in kept}
        candidates = [sid for sid in sorted(in_pool)
                      if sid not in tranche.positions and day in self.tri.get(sid, {})]
        if self.mode == 'random_size':
            buys = self._random_size_buys(tranche, day, range_secs, kept, used_issuer)
        else:
            buys = []
            for sid in sorted(candidates, key=lambda sid: _hash_key(self.seed, day, sid)):
                if len(kept) + len(buys) >= self.N:
                    break
                issuer = self.issuer.get(sid, '')
                if issuer and issuer in used_issuer:
                    continue
                buys.append(sid)
                used_issuer.add(issuer)
        return dict(sells=sells, buys=buys, rank={}, keeps=kept, scores={})

    def _random_size_buys(self, tranche: Tranche, day: str, range_secs: list[dict],
                          kept: list[str], used_issuer: set[str]) -> list[str]:
        needed = self.N - len(kept)
        if needed <= 0:
            return []
        groups = self._size_groups(range_secs)
        group_of = {sid: g for g, sids in enumerate(groups) for sid in sids}
        base = self.buy_plan.get(f'{day}|{tranche.index}', [])
        available = [s['security_id'] for s in range_secs
                     if s['security_id'] not in tranche.positions and day in self.tri.get(s['security_id'], {})]
        buys: list[str] = []

        def free(sid: str) -> bool:
            issuer = self.issuer.get(sid, '')
            return sid not in buys and not (issuer and issuer in used_issuer)

        for slot in range(needed):
            target = group_of.get(base[slot]) if slot < len(base) else None
            chosen = None
            if target is not None:
                order = []
                for distance in range(5):
                    for group in (target - distance, target + distance):
                        if 0 <= group < 5 and group not in order:
                            order.append(group)
                for group in order:
                    pool = [sid for sid in available if group_of.get(sid) == group and free(sid)]
                    if pool:
                        chosen = min(pool, key=lambda sid: _hash_key(self.seed, day, sid))
                        if group != target:
                            self.size_fallback += 1
                        break
            if chosen is None:
                pool = [sid for sid in available if free(sid)]
                if not pool:
                    break
                chosen = min(pool, key=lambda sid: _hash_key(self.seed, day, sid))
                self.size_fallback += 1
            buys.append(chosen)
            issuer = self.issuer.get(chosen, '')
            if issuer:
                used_issuer.add(issuer)
        return buys

    def _pool_targets(self, day: str) -> dict[str, float]:
        info = self.pools.get(day)
        if info is None:
            return {}
        best: dict[str, tuple[float, str]] = {}
        for sid, row in info['amounts'].items():
            issuer = row['issuer_id']
            amount = float(row['amount_cents'])
            if issuer not in best or amount > best[issuer][0]:
                best[issuer] = (amount, sid)
        chosen = {sid: amount for amount, sid in best.values()}
        top_n = 10 if self.mode == 'top10_cw' else (self.top_n if self.mode == 'pool' else None)
        if top_n and len(chosen) > top_n:
            chosen = dict(sorted(chosen.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n])
        if not chosen:
            return {}
        weighting = ('ew' if self.mode == 'pool_ew' else
                     ('cw' if self.mode in ('pool_cw', 'top10_cw') else self.weighting))
        if weighting == 'ew':
            return {sid: 1.0 / len(chosen) for sid in chosen}
        total = sum(chosen.values())
        return {sid: value / total for sid, value in chosen.items()}

    # -- execution ----------------------------------------------------------------------------------------------
    def _apply_actions(self, day: str) -> None:
        if not self.action_sids:
            return
        done = []
        for sid in self.action_sids:
            rows = self.actions[sid]
            while self.applied[sid] < len(rows) and rows[self.applied[sid]]['action_date'] <= day:
                row = rows[self.applied[sid]]
                self.applied[sid] += 1
                for tranche in self.tranches:
                    shares = tranche.positions.get(sid)
                    if not shares:
                        continue
                    if row['action_type'] in ('split', 'reverse_split') and row['ratio']:
                        tranche.positions[sid] = shares * float(row['ratio'])
                    elif row['action_type'] in ('cash_dividend', 'special_dividend') and row['cash_amount']:
                        amount = shares * float(row['cash_amount'])
                        tranche.cash += amount
                        issuer = tranche.issuer_of.get(sid, '')
                        tranche.income[issuer] = tranche.income.get(issuer, 0.0) + amount
                    elif row['action_type'] == 'spinoff':
                        value = self.spin_value.get((sid, row['action_date']))
                        if value:
                            amount = shares * value
                            tranche.cash += amount
                            issuer = tranche.issuer_of.get(sid, '')
                            tranche.income[issuer] = tranche.income.get(issuer, 0.0) + amount
            if self.applied[sid] >= len(rows):
                done.append(sid)
        for sid in done:
            self.action_sids.remove(sid)

    def _execute(self, day: str) -> None:
        if not self.pending:
            return
        remaining: list[dict] = []
        for order in [o for o in self.pending if o['side'] == 'sell']:
            tranche = self.tranches[order['tranche']]
            sid = order['security_id']
            price = self.opens.get(sid, {}).get(day)
            held = tranche.positions.get(sid, 0.0)
            if held <= 0:
                continue
            shares = held if order.get('shares') is None else min(held, float(order['shares']))
            if price is None or price <= 0:
                remaining.append(order)
                continue
            if shares <= 0:
                continue
            fill = price * (1 - self.slip)
            proceeds = shares * fill - self.fee
            tranche.cash += proceeds
            if shares >= held - 1e-12:
                tranche.positions.pop(sid, None)
                issuer = tranche.issuer_of.pop(sid, '')
                cost = tranche.cost.pop(sid, 0.0)
            else:
                tranche.positions[sid] = held - shares
                issuer = tranche.issuer_of.get(sid, '')
                cost = tranche.cost.get(sid, 0.0) * (shares / held)
                tranche.cost[sid] = tranche.cost.get(sid, 0.0) - cost
            tranche.sell_proceeds[issuer] = tranche.sell_proceeds.get(issuer, 0.0) + proceeds
            tranche.buy_cost[issuer] = tranche.buy_cost.get(issuer, 0.0) + cost
            tranche.trades += 1
            self._record_trade(day, order, sid, 'sell', shares, fill)
        by_tranche: dict[int, list[dict]] = defaultdict(list)
        for order in [o for o in self.pending if o['side'] == 'buy']:
            by_tranche[order['tranche']].append(order)
        for index, orders in by_tranche.items():
            tranche = self.tranches[index]
            weight_sum = sum(o.get('weight', 0.0) for o in orders) or 1.0
            available = tranche.cash
            notionals = sum(o['notional'] for o in orders if o.get('notional') is not None)
            scale = min(1.0, available / notionals) if notionals > 0 else 1.0
            for order in orders:
                if self.mode == 'random' and len(tranche.positions) >= self.N:
                    # an exit without an opening price is deferred, so the book already holds N names: stop buying
                    continue
                sid = order['security_id']
                price = self.opens.get(sid, {}).get(day)
                if price is None or price <= 0:
                    remaining.append(order)
                    continue
                if order.get('notional') is not None:
                    budget = order['notional'] * scale
                else:
                    budget = available * order['weight'] / weight_sum
                fill = price * (1 + self.slip)
                if budget <= self.fee:
                    continue
                shares = (budget - self.fee) / fill
                if not self.fractional:
                    shares = float(math.floor(shares))
                if shares <= 0:
                    continue
                cost = shares * fill + self.fee
                if cost > tranche.cash + 1e-9:
                    shares = (tranche.cash - self.fee) / fill
                    if not self.fractional:
                        shares = float(math.floor(shares))
                    if shares <= 0:
                        continue
                    cost = shares * fill + self.fee
                tranche.cash -= cost
                tranche.positions[sid] = tranche.positions.get(sid, 0.0) + shares
                tranche.cost[sid] = tranche.cost.get(sid, 0.0) + cost
                issuer = self.issuer.get(sid, '')
                tranche.issuer_of[sid] = issuer
                tranche.buy_cost[issuer] = tranche.buy_cost.get(issuer, 0.0) + cost
                tranche.trades += 1
                self._record_trade(day, order, sid, 'buy', shares, fill)
        self.pending = remaining

    def _record_trade(self, day, order, sid, side, shares, fill):
        if self.first_trade_day is None:
            self.first_trade_day = day
        self.trades.append(dict(date=day, tranche=order['tranche'], security_id=sid, side=side,
                                shares=round(shares, 8), price=round(fill, 8), fee=self.fee,
                                reason=order['reason'], signal_date=order['signal_date']))

    def _queue_momentum(self, tranche: Tranche, day: str) -> None:
        pool = self.pools[day]
        orders = (self._random_orders(tranche, day, pool) if self.mode in ('random', 'random_size')
                  else self._momentum_orders(tranche, day, pool))
        rank, scores = orders['rank'], orders['scores']
        for sid in orders['keeps']:
            self.decisions.append(dict(signal_date=day, tranche=tranche.index, security_id=sid, action='keep',
                                       rank=rank.get(sid, ''), score=scores.get(sid, '')))
        for sid in orders['sells']:
            self.decisions.append(dict(signal_date=day, tranche=tranche.index, security_id=sid, action='sell',
                                       rank=rank.get(sid, ''), score=scores.get(sid, '')))
        for sid in orders['buys']:
            self.decisions.append(dict(signal_date=day, tranche=tranche.index, security_id=sid, action='buy',
                                       rank=rank.get(sid, ''), score=scores.get(sid, '')))
        if self.rebalance == 'R1':
            # restore equal weight over the post-trade book, and only on a check day that changes a holding
            if orders['sells'] or orders['buys']:
                target = sorted(set(orders['keeps']) | set(orders['buys']))
                nxt = bisect.bisect_right(self.calendar, day)
                if target and nxt < len(self.calendar):
                    self._queue_target_values(tranche, day, {sid: 1.0 / len(target) for sid in target},
                                              'rebalance', record=False, price_day=self.calendar[nxt])
            return
        for sid in orders['sells']:
            self.pending.append(dict(tranche=tranche.index, security_id=sid, side='sell', reason='signal',
                                     signal_date=day, weight=0.0))
        if orders['buys']:
            weights = self._buy_weights(day, orders['buys'])
            for sid in orders['buys']:
                self.pending.append(dict(tranche=tranche.index, security_id=sid, side='buy', reason='signal',
                                         signal_date=day, weight=weights[sid]))

    def _buy_weights(self, day: str, buys: list[str]) -> dict[str, float]:
        """Equal split, or a split proportional to the fund amount of each new buy (buy_weighting='amount')."""
        if not buys:
            return {}
        if self.buy_weighting != 'amount':
            return {sid: 1.0 / len(buys) for sid in buys}
        amounts = {sid: self.amounts.get(day, {}).get(sid, 0.0) for sid in buys}
        total = sum(amounts.values())
        if total <= 0:
            return {sid: 1.0 / len(buys) for sid in buys}
        return {sid: amounts[sid] / total for sid in buys}

    def _queue_pool(self, tranche: Tranche, day: str) -> None:
        """Rebalance to the new target at the opening price (amendment 1)."""
        self._queue_target_values(tranche, day, self._pool_targets(day), 'version')

    def _queue_target_values(self, tranche: Tranche, day: str, target: dict[str, float], reason: str,
                             record: bool = True, price_day: str | None = None) -> None:
        """Trade only the difference between each target value and the current value at the opening price.

        Target value = weight x the account's equity at the opening price. Exits come first, then the differences;
        a difference of zero (within a micro-dollar) is not traded. `record` writes an order row to
        weekly_decisions.csv (pool versions do; an R1 rebalance of already-decided holdings does not).
        """
        if not target:
            return
        self.pending = [o for o in self.pending if o['tranche'] != tranche.index or o['side'] == 'sell']
        price_day = day if price_day is None else price_day
        prices = {sid: self.opens.get(sid, {}).get(price_day) for sid in set(tranche.positions) | set(target)}
        equity = tranche.cash
        for sid, shares in tranche.positions.items():
            price = prices.get(sid)
            equity += shares * (price if price is not None else (self.close_on(sid, price_day) or 0.0))
        tolerance = 1e-6
        target_value = {sid: weight * equity for sid, weight in target.items()}
        for sid in sorted(tranche.positions):
            price = prices.get(sid)
            want = target_value.get(sid, 0.0)
            if price is None or price <= 0:
                if want <= 0.0:                      # an exit without a price waits for the next session
                    if record:
                        self.decisions.append(dict(signal_date=day, tranche=tranche.index, security_id=sid,
                                                   action='sell', rank='', score=''))
                    self.pending.append(dict(tranche=tranche.index, security_id=sid, side='sell',
                                             reason=reason, signal_date=day, shares=None))
                continue
            delta = want - tranche.positions[sid] * price
            if delta < -tolerance:
                shares = min(tranche.positions[sid], -delta / price)
                if record:
                    self.decisions.append(dict(signal_date=day, tranche=tranche.index, security_id=sid,
                                               action='sell', rank='', score=''))
                self.pending.append(dict(tranche=tranche.index, security_id=sid, side='sell', reason=reason,
                                         signal_date=day, shares=shares))
            elif delta > tolerance:
                if record:
                    self.decisions.append(dict(signal_date=day, tranche=tranche.index, security_id=sid,
                                               action='buy', rank='', score=''))
                self.pending.append(dict(tranche=tranche.index, security_id=sid, side='buy', reason=reason,
                                         signal_date=day, notional=delta))
        for sid in sorted(target):
            if sid in tranche.positions:
                continue
            price = prices.get(sid)
            if price is None or price <= 0 or target_value[sid] <= tolerance:
                continue
            if record:
                self.decisions.append(dict(signal_date=day, tranche=tranche.index, security_id=sid,
                                           action='buy', rank='', score=''))
            self.pending.append(dict(tranche=tranche.index, security_id=sid, side='buy', reason=reason,
                                     signal_date=day, notional=target_value[sid]))

    # -- main loop ----------------------------------------------------------------------------------------------
    def run(self) -> dict:
        self.load()
        self.tranches = [Tranche(index=j, cash=self.capital / self.K) for j in range(self.K)]
        signal_index = {day: i for i, day in enumerate(self.signal_days)}
        for day in self.calendar:
            self._apply_actions(day)
            if self.mode in POOL_MODES:
                # a new pool version is held from the opening price of its first session
                if day in self.pool_days:
                    for tranche in self.tranches:
                        self._queue_pool(tranche, day)
            self._execute(day)
            if self.mode == 'voo':
                pass
            elif self.mode in POOL_MODES:
                pass
            elif day in signal_index:
                week = signal_index[day]
                for tranche in self.tranches:
                    if week < tranche.index or (week - tranche.index) % self.K:
                        continue
                    self._queue_momentum(tranche, day)
            self._mark(day)
        return self.result()

    def _mark(self, day: str) -> None:
        if self.mode == 'voo' and self.bench_units == 0.0:
            tri = self.tri.get(BENCH, {}).get(day)
            if tri:
                self.bench_entry = day
                self.bench_units = self.capital / tri
        if self.mode == 'voo':
            bench = self.bench_units * self.tri.get(BENCH, {}).get(day, 0.0)
            self.daily.append(dict(date=day, cash=0.0, positions_value=bench, equity=bench, benchmark=bench))
            return
        for tranche in self.tranches:
            for sid in tranche.positions:
                value = self.closes.get(sid, {}).get(day)
                if value is not None:
                    self.last_close[sid] = value
        cash = sum(t.cash for t in self.tranches)
        value = 0.0
        for tranche in self.tranches:
            for sid, shares in tranche.positions.items():
                close = self.close_on(sid, day)
                value += shares * (close or 0.0)
                if close:
                    self.positions_today.append(dict(date=day, tranche=tranche.index, security_id=sid,
                                                     shares=round(shares, 8), close=close,
                                                     value=round(shares * close, 6)))
        if self.bench_units == 0.0 and self.first_trade_day is not None and day == self.first_trade_day:
            tri = self.tri.get(BENCH, {}).get(day)
            close = self.closes.get(BENCH, {}).get(day)
            open_price = self.opens.get(BENCH, {}).get(day)
            if tri and close and open_price:
                self.bench_entry = day
                self.bench_units = self.capital / (tri * open_price / close)
        bench = self.bench_units * self.tri.get(BENCH, {}).get(day, 0.0) if self.bench_units else 0.0
        self.daily.append(dict(date=day, cash=cash, positions_value=value, equity=cash + value, benchmark=bench))
        self.position_rows.extend(self.positions_today)
        self.positions_today = []

    def result(self) -> dict:
        pnl: dict[str, float] = defaultdict(float)
        last_day = self.calendar[-1]
        for tranche in self.tranches:
            for issuer in set(tranche.buy_cost) | set(tranche.sell_proceeds) | set(tranche.income):
                pnl[issuer] += (tranche.sell_proceeds.get(issuer, 0.0) + tranche.income.get(issuer, 0.0)
                                - tranche.buy_cost.get(issuer, 0.0))
            for sid, shares in tranche.positions.items():
                issuer = tranche.issuer_of.get(sid, '')
                pnl[issuer] += shares * (self.close_on(sid, last_day) or 0.0) - tranche.cost.get(sid, 0.0)
        return dict(daily=self.daily, trades=self.trades, decisions=self.decisions, pnl=dict(pnl),
                    positions=self.position_rows, issuers=dict(self.issuer), config=self.cfg,
                    size_fallback=self.size_fallback)

    def _positions(self) -> list[dict]:
        rows = []
        for item in self.daily:
            day = item['date']
            for tranche in self.tranches:
                for sid, shares in tranche.positions.items():
                    close = self.close_on(sid, day)
                    if close is None:
                        continue
                    rows.append(dict(date=day, tranche=tranche.index, security_id=sid, shares=round(shares, 8),
                                     close=close, value=round(shares * close, 6)))
        return rows
