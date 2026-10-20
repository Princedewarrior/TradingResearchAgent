"""Safe MT5 demo preflight adapter.

This module deliberately contains no order_send/order_modify/order_close code.
It can only inspect a locally running MT5 terminal and refuses anything other
than a demo account. A separate, reviewed execution module would be required
before any demo order can ever be sent.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .config import Settings


@dataclass(frozen=True)
class Mt5Preflight:
    ready: bool
    reason: str
    account: dict[str, Any] | None = None
    symbol: dict[str, Any] | None = None


def preflight(settings: Settings) -> Mt5Preflight:
    """Inspect only; does not authenticate, trade, or modify MT5 state."""
    if not settings.mt5_enabled:
        return Mt5Preflight(False, 'MT5 is disabled in configuration.')
    try:
        import MetaTrader5 as mt5  # Optional Windows-only runtime dependency.
    except ImportError:
        return Mt5Preflight(False, 'MetaTrader5 Python package is not installed.')
    kwargs = {'path': settings.mt5_terminal_path} if settings.mt5_terminal_path else {}
    if not mt5.initialize(**kwargs):
        return Mt5Preflight(False, f'MT5 terminal initialization failed: {mt5.last_error()}')
    try:
        info = mt5.account_info()
        if info is None:
            return Mt5Preflight(False, 'No MT5 account is logged in.')
        demo_mode = getattr(mt5, 'ACCOUNT_TRADE_MODE_DEMO', None)
        if getattr(info, 'trade_mode', None) != demo_mode:
            return Mt5Preflight(False, 'Refusing connection: MT5 account is not a demo account.')
        symbol = mt5.symbol_info(settings.mt5_symbol)
        tick = mt5.symbol_info_tick(settings.mt5_symbol)
        return Mt5Preflight(True, 'Demo MT5 account verified; read-only inspection is ready.', {
            'login': info.login, 'server': info.server, 'balance': info.balance,
            'equity': info.equity, 'currency': info.currency, 'leverage': info.leverage,
        }, None if symbol is None else {
            'name': symbol.name, 'visible': symbol.visible, 'trade_mode': symbol.trade_mode,
            'volume_min': symbol.volume_min, 'volume_step': symbol.volume_step,
            'point': symbol.point, 'bid': tick.bid if tick else None, 'ask': tick.ask if tick else None,
        })
    finally:
        mt5.shutdown()


if __name__ == '__main__':
    import json
    from .config import settings
    print(json.dumps(asdict(preflight(settings)), indent=2, default=str))
