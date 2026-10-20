"""Chronological, long-only research backtest for the BTC scalp hypothesis."""
from __future__ import annotations

from dataclasses import dataclass
from .data import Candle
from .strategy import btc_evaluation


@dataclass(frozen=True)
class BacktestResult:
    candle_count: int
    trade_count: int
    net_pnl: float
    net_return_pct: float
    win_rate_pct: float | None
    average_win: float | None
    average_loss: float | None
    max_drawdown_pct: float
    fees_paid: float
    slippage_cost: float


def run_btc_scalp_backtest(candles: list[Candle], settings) -> BacktestResult:
    """Test the unchanged entry hypothesis without peeking into future bars.

    An evaluation occurs at a completed candle, so its order can fill only at
    the next candle's open. Fees and slippage are applied on both sides.
    """
    cash = float(settings.starting_cash)
    starting_cash = cash
    position = None
    pending = None
    pnls: list[float] = []
    fees_paid = 0.0
    slippage_cost = 0.0
    peak_equity = cash
    max_drawdown = 0.0

    def close_position(active, raw_price: float) -> None:
        nonlocal cash, fees_paid, slippage_cost
        fill = raw_price * (1 - settings.slippage_bps / 10_000)
        exit_fee = active['qty'] * fill * settings.fee_rate
        proceeds = active['qty'] * fill - exit_fee
        cash += proceeds
        pnls.append(proceeds - active['cost'])
        fees_paid += exit_fee
        slippage_cost += active['qty'] * (raw_price - fill)

    for index, candle in enumerate(candles):
        if pending is not None:
            stop, take = pending
            fill = candle.open * (1 + settings.slippage_bps / 10_000)
            risk = max(fill - stop, 0.0)
            qty = 0.0 if risk <= 0 else (cash * settings.max_risk_pct) / (risk + fill * settings.fee_rate)
            entry_fee = qty * fill * settings.fee_rate
            cost = qty * fill + entry_fee
            if qty > 0 and cost <= cash:
                cash -= cost
                fees_paid += entry_fee
                slippage_cost += qty * (fill - candle.open)
                position = {'qty': qty, 'stop': stop, 'take': take, 'cost': cost,
                            'opened_index': index, 'peak': fill}
            pending = None

        if position is not None:
            # Only a stop known before this bar can trigger inside it. The bar
            # high is applied to trailing protection only after it survives.
            hit_stop = candle.low <= position['stop']
            hit_take = candle.high >= position['take']
            timed_out = index - position['opened_index'] >= settings.scalp_max_hold_minutes
            if hit_stop:
                close_position(position, position['stop'])
                position = None
            elif hit_take:
                close_position(position, position['take'])
                position = None
            elif timed_out:
                close_position(position, candle.close)
                position = None
            else:
                peak = max(position['peak'], candle.high)
                protective = max(position['stop'], peak * (1 - settings.trailing_stop_pct))
                if peak >= position['cost'] / position['qty'] * (1 + settings.breakeven_trigger_pct):
                    protective = max(protective, position['cost'] / position['qty'])
                position['peak'] = peak
                position['stop'] = protective

        # Decision at index cannot fill until the next loop iteration.
        if position is None and pending is None and index >= 29 and index + 1 < len(candles):
            evaluation = btc_evaluation(candles[:index + 1], settings.scalp_stop_pct, settings.scalp_take_pct)
            if evaluation['action'] == 'BUY':
                pending = (evaluation['stop'], evaluation['take'])

        equity = cash + (position['qty'] * candle.close if position else 0.0)
        peak_equity = max(peak_equity, equity)
        if peak_equity:
            max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity * 100)

    if position is not None:
        close_position(position, candles[-1].close)

    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    return BacktestResult(
        candle_count=len(candles), trade_count=len(pnls), net_pnl=sum(pnls),
        net_return_pct=((cash - starting_cash) / starting_cash * 100) if starting_cash else 0.0,
        win_rate_pct=(len(wins) / len(pnls) * 100) if pnls else None,
        average_win=(sum(wins) / len(wins)) if wins else None,
        average_loss=(sum(losses) / len(losses)) if losses else None,
        max_drawdown_pct=max_drawdown, fees_paid=fees_paid, slippage_cost=slippage_cost,
    )
