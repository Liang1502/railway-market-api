#!/usr/bin/env python3
"""Compatibility wrapper for the current AI scanner backtest.

The old Railway project backtest depended on scanner_v7.py. That module has
been replaced by stock_scanner/ai_scanner.py and its backtest entrypoint, so
this script forwards backtest commands to stock_scanner/run.py.
"""
from __future__ import annotations

import argparse
import bisect
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


STOCK_SCANNER_DIR = Path(
    os.environ.get("STOCK_SCANNER_DIR", "/Users/chiachun/Desktop/stock_scanner")
)


@dataclass(frozen=True)
class BacktestConfig:
    period_days: int = 548
    warmup_days: int = 60
    stop_loss: float = 0.05
    take_profit_1: float = 0.08
    take_profit_2: float = 0.15
    atr_stop_mult: float = 1.5
    gap_skip: float = 0.03
    max_hold_days: int = 20
    cost_swing: float = 0.0038
    cost_daytrade: float = 0.0023
    capital_per_trade: float = 100_000
    starting_capital: float = 3_000_000
    max_concurrent: int = 20
    min_score: int = 15
    yoy_max: float = 500.0
    revenue_lag_days: int = 15


@dataclass(frozen=True)
class Trade:
    symbol: str
    score: int
    signal_date: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    hold_days: int
    gross_return: float
    net_return: float
    cost: float
    exit_reason: str
    is_daytrade: bool
    yoy: float | None = None
    industry: str = ""


def build_yoy_schedule(rows: list[dict], lag_days: int) -> list[tuple[pd.Timestamp, float]]:
    by_month: dict[tuple[int, int], float] = {}
    out: list[tuple[pd.Timestamp, float]] = []
    for row in sorted(rows, key=lambda r: str(r.get("date", ""))):
        dt = pd.Timestamp(row.get("date"))
        yoy = row.get("yoy_pct")
        revenue = row.get("revenue")
        if yoy is None and revenue is not None:
            prev = by_month.get((dt.year - 1, dt.month))
            if prev:
                yoy = (float(revenue) - prev) / prev * 100
        if revenue is not None:
            by_month[(dt.year, dt.month)] = float(revenue)
        if yoy is not None:
            out.append((dt + pd.Timedelta(days=lag_days), float(yoy)))
    return sorted(out, key=lambda x: x[0])


def yoy_at_date(schedule: list[tuple[pd.Timestamp, float]], when: pd.Timestamp) -> float | None:
    if not schedule:
        return None
    dates = [x[0] for x in schedule]
    idx = bisect.bisect_right(dates, pd.Timestamp(when)) - 1
    return schedule[idx][1] if idx >= 0 else None


def build_taiex_schedule(df: pd.DataFrame) -> list[tuple[pd.Timestamp, bool]]:
    out = []
    for _, row in df.sort_values("date").iterrows():
        close = float(row["close"])
        ma20 = float(row["MA20"])
        ma60 = float(row["MA60"])
        out.append((pd.Timestamp(row["date"]), close >= ma20 and close >= ma60))
    return out


def is_taiex_bullish(schedule: list[tuple[pd.Timestamp, bool]], when: pd.Timestamp) -> bool:
    if not schedule:
        return True
    dates = [x[0] for x in schedule]
    idx = bisect.bisect_right(dates, pd.Timestamp(when)) - 1
    return schedule[idx][1] if idx >= 0 else True


def _finish_trade(
    symbol: str,
    score: int,
    signal_date: str,
    entry_date,
    exit_date,
    entry_price: float,
    exit_price: float,
    gross_return: float,
    exit_reason: str,
    yoy: float | None,
    cfg: BacktestConfig,
) -> Trade:
    hold_days = max(1, (pd.Timestamp(exit_date) - pd.Timestamp(entry_date)).days + 1)
    is_daytrade = pd.Timestamp(exit_date).date() == pd.Timestamp(entry_date).date()
    cost = cfg.cost_daytrade if is_daytrade else cfg.cost_swing
    return Trade(
        symbol=symbol,
        score=score,
        signal_date=str(signal_date),
        entry_date=str(pd.Timestamp(entry_date).date()),
        exit_date=str(pd.Timestamp(exit_date).date()),
        entry_price=float(entry_price),
        exit_price=float(exit_price),
        hold_days=hold_days,
        gross_return=float(gross_return),
        net_return=float(gross_return) - cost,
        cost=cost,
        exit_reason=exit_reason,
        is_daytrade=is_daytrade,
        yoy=yoy,
    )


def simulate_trade(
    df: pd.DataFrame,
    entry_idx: int,
    signal_date: str,
    symbol: str,
    score: int,
    yoy: float | None,
    cfg: BacktestConfig,
    atr: float | None = None,
) -> Trade | None:
    if entry_idx <= 0 or entry_idx >= len(df):
        return None

    entry_row = df.iloc[entry_idx]
    prev_close = float(df.iloc[entry_idx - 1]["close"])
    entry_price = float(entry_row["open"])
    if prev_close > 0 and (entry_price / prev_close - 1) > cfg.gap_skip:
        return None

    stop_pct = cfg.stop_loss
    if atr and entry_price:
        stop_pct = max(stop_pct, (float(atr) * cfg.atr_stop_mult) / entry_price)
    stop_price = entry_price * (1 - stop_pct)
    tp1 = entry_price * (1 + cfg.take_profit_1)
    tp2 = entry_price * (1 + cfg.take_profit_2)
    entry_date = entry_row["date"]
    phase2 = False

    last_idx = min(len(df) - 1, entry_idx + cfg.max_hold_days - 1)
    for idx in range(entry_idx, last_idx + 1):
        row = df.iloc[idx]
        open_ = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        current_date = row["date"]

        if idx > entry_idx and open_ <= stop_price:
            gross = open_ / entry_price - 1
            return _finish_trade(symbol, score, signal_date, entry_date, current_date, entry_price, open_, gross, "stop_loss_gap", yoy, cfg)
        if low <= stop_price:
            reason = "stop_loss_sameday" if idx == entry_idx else "stop_loss"
            gross = stop_price / entry_price - 1
            return _finish_trade(symbol, score, signal_date, entry_date, current_date, entry_price, stop_price, gross, reason, yoy, cfg)

        if not phase2 and high >= tp1:
            phase2 = True
            stop_price = entry_price
            if high >= tp2:
                reason = "take_profit_sameday" if idx == entry_idx else "take_profit"
                gross = 0.5 * cfg.take_profit_1 + 0.5 * cfg.take_profit_2
                return _finish_trade(symbol, score, signal_date, entry_date, current_date, entry_price, tp2, gross, reason, yoy, cfg)
            continue
        if phase2:
            if open_ <= entry_price or low <= entry_price:
                gross = 0.5 * cfg.take_profit_1
                return _finish_trade(symbol, score, signal_date, entry_date, current_date, entry_price, entry_price, gross, "breakeven_stop", yoy, cfg)
            if high >= tp2:
                gross = 0.5 * cfg.take_profit_1 + 0.5 * cfg.take_profit_2
                return _finish_trade(symbol, score, signal_date, entry_date, current_date, entry_price, tp2, gross, "take_profit", yoy, cfg)

        if idx == last_idx:
            gross = close / entry_price - 1
            return _finish_trade(symbol, score, signal_date, entry_date, current_date, entry_price, close, gross, "time_stop", yoy, cfg)
    return None


def apply_position_limit(trades: list[Trade], max_concurrent: int) -> list[Trade]:
    accepted: list[Trade] = []
    for trade in sorted(trades, key=lambda t: (t.entry_date, -t.score)):
        active = [
            t for t in accepted
            if pd.Timestamp(t.entry_date) <= pd.Timestamp(trade.entry_date) <= pd.Timestamp(t.exit_date)
        ]
        if len(active) < max_concurrent:
            accepted.append(trade)
    return sorted(accepted, key=lambda t: (t.entry_date, t.symbol))


def compute_max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = equity / peak - 1
    return float(dd.min())


def compute_max_concurrent(df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    events: list[tuple[pd.Timestamp, int]] = []
    for _, row in df.iterrows():
        events.append((pd.Timestamp(row["entry_date"]), 1))
        events.append((pd.Timestamp(row["exit_date"]) + pd.Timedelta(days=1), -1))
    active = max_active = 0
    for _, delta in sorted(events):
        active += delta
        max_active = max(max_active, active)
    return max_active


def analyze(trades: list[Trade], cfg: BacktestConfig) -> dict:
    if not trades:
        return {"total_trades": 0, "note": "no trades"}
    returns = pd.Series([t.net_return for t in trades], dtype=float)
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    total_pnl = float((returns * cfg.capital_per_trade).sum())
    equity = cfg.starting_capital + (returns * cfg.capital_per_trade).cumsum()
    profit_factor = float(wins.sum() / abs(losses.sum())) if abs(losses.sum()) > 0 else 99.9
    avg = float(returns.mean())
    std = float(returns.std(ddof=0))
    return {
        "total_trades": len(trades),
        "win_rate": float((returns > 0).mean()),
        "avg_win_pct": float(wins.mean() * 100) if len(wins) else 0.0,
        "avg_loss_pct": float(losses.mean() * 100) if len(losses) else 0.0,
        "profit_factor": profit_factor,
        "total_pnl": total_pnl,
        "annual_return_pct": total_pnl / cfg.starting_capital * 100,
        "max_drawdown_pct": compute_max_drawdown(equity) * 100,
        "sharpe_ratio": (avg / std * (252 ** 0.5)) if std else 0.0,
        "daytrade_ratio": sum(t.is_daytrade for t in trades) / len(trades),
        "avg_hold_days": sum(t.hold_days for t in trades) / len(trades),
        "avg_return_pct": avg * 100,
        "exit_breakdown": dict(Counter(t.exit_reason for t in trades)),
        "by_score_bucket": {},
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="轉呼叫 stock_scanner 的新版 AI 三策略回測",
    )
    ap.add_argument("--strategy", choices=["A", "B", "C", "all"], default="all")
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--walkforward", action="store_true")
    ap.add_argument("--fold", type=int, default=3)
    ap.add_argument("--hold", type=int, default=None, help="新版 run.py backtest 目前不使用，保留相容")
    ap.add_argument("--symbols", default="", help="新版三策略回測使用 ai_universe.csv，保留相容")
    ap.add_argument("--no-cache", action="store_true", help="新版三策略回測使用本地 cache，保留相容")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_py = STOCK_SCANNER_DIR / "run.py"
    if not run_py.exists():
        print(f"找不到新版回測入口：{run_py}", file=sys.stderr)
        return 1

    if args.hold is not None:
        print("提示：新版 stock_scanner/run.py backtest 目前不支援 --hold，已忽略。")
    if args.symbols:
        print("提示：新版三策略回測使用 ai_universe.csv，不支援 --symbols，已忽略。")
    if args.no_cache:
        print("提示：新版三策略回測使用本地 cache，不支援 --no-cache，已忽略。")

    cmd = [
        sys.executable,
        str(run_py),
        "backtest",
        "--strategy",
        args.strategy,
        "--days",
        str(args.days),
    ]
    if args.walkforward:
        cmd += ["--walkforward", "--fold", str(args.fold)]

    return subprocess.call(cmd, cwd=STOCK_SCANNER_DIR)


if __name__ == "__main__":
    raise SystemExit(main())
