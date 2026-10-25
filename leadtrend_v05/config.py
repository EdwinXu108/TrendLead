"""Bootstrap configuration for the v0.5 stage-1 offline demo."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BootstrapConfig:
    dataset_kind: str
    L: int
    N: int
    k: int
    b: float
    initial_capital: float
    fee_per_trade: float
    slippage_bps: float
    stress_slippage_bps: float
    class_selection_turnover_days: int
    benchmark_security_id: str
    input_dir: str
    timezone_internal: str
    timezone_display: str
    currency: str
    supported_events: tuple[str, ...]
    unsupported_event_types: tuple[str, ...]
    excluded_security_types: tuple[str, ...]
    excluded_tickers: tuple[str, ...]
    rebalance_policy: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def slippage_rate(self) -> float:
        return self.slippage_bps / 10_000.0

    @property
    def stress_slippage_rate(self) -> float:
        return self.stress_slippage_bps / 10_000.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_kind": self.dataset_kind,
            "L": self.L,
            "N": self.N,
            "k": self.k,
            "b": self.b,
            "initial_capital": self.initial_capital,
            "fee_per_trade": self.fee_per_trade,
            "slippage_bps": self.slippage_bps,
            "stress_slippage_bps": self.stress_slippage_bps,
            "class_selection_turnover_days": self.class_selection_turnover_days,
            "benchmark_security_id": self.benchmark_security_id,
            "input_dir": self.input_dir,
            "timezone_internal": self.timezone_internal,
            "timezone_display": self.timezone_display,
            "currency": self.currency,
            "supported_events": list(self.supported_events),
            "unsupported_event_types": list(self.unsupported_event_types),
            "excluded_security_types": list(self.excluded_security_types),
            "excluded_tickers": list(self.excluded_tickers),
            "rebalance_policy": self.rebalance_policy,
        }


DEFAULTS: dict[str, Any] = {
    "dataset_kind": "synthetic",
    "L": 20,
    "N": 10,
    "k": 3,
    "b": 2,
    "initial_capital": 100000.0,
    "fee_per_trade": 1.0,
    "slippage_bps": 5.0,
    "stress_slippage_bps": 10.0,
    "class_selection_turnover_days": 20,
    "benchmark_security_id": "SYN_VOO",
    "input_dir": "data/v05/fixtures",
    "timezone_internal": "UTC",
    "timezone_display": "America/New_York",
    "currency": "USD",
    "supported_events": ["split", "reverse_split", "cash_dividend"],
    "unsupported_event_types": [
        "spinoff",
        "merger",
        "acquisition",
        "delisting",
        "name_change",
        "symbol_change",
    ],
    "excluded_security_types": [
        "reit",
        "spac",
        "shell",
        "partnership_unit",
        "etf",
        "fund",
        "bond",
        "preferred_fixed",
        "warrant",
    ],
    "excluded_tickers": ["BRK.A", "BRKA"],
    "rebalance_policy": "only_on_swap",
}


def load_config(path: str | Path) -> BootstrapConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    merged = dict(DEFAULTS)
    extra = {k: v for k, v in raw.items() if k not in DEFAULTS}
    merged.update({k: raw[k] for k in raw if k in DEFAULTS})
    return BootstrapConfig(
        dataset_kind=str(merged["dataset_kind"]),
        L=int(merged["L"]),
        N=int(merged["N"]),
        k=int(merged["k"]),
        b=float(merged["b"]),
        initial_capital=float(merged["initial_capital"]),
        fee_per_trade=float(merged["fee_per_trade"]),
        slippage_bps=float(merged["slippage_bps"]),
        stress_slippage_bps=float(merged["stress_slippage_bps"]),
        class_selection_turnover_days=int(merged["class_selection_turnover_days"]),
        benchmark_security_id=str(merged["benchmark_security_id"]),
        input_dir=str(merged["input_dir"]),
        timezone_internal=str(merged["timezone_internal"]),
        timezone_display=str(merged["timezone_display"]),
        currency=str(merged["currency"]),
        supported_events=tuple(merged["supported_events"]),
        unsupported_event_types=tuple(merged["unsupported_event_types"]),
        excluded_security_types=tuple(merged["excluded_security_types"]),
        excluded_tickers=tuple(merged["excluded_tickers"]),
        rebalance_policy=str(merged["rebalance_policy"]),
        extra=extra,
    )
