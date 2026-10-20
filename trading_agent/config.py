from __future__ import annotations
from dataclasses import dataclass
import os
from pathlib import Path
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(path, *_args, **_kwargs):
        """Minimal local .env reader used only when python-dotenv is absent."""
        try:
            for raw in Path(path).read_text(encoding='utf-8').splitlines():
                line=raw.strip()
                if not line or line.startswith('#') or '=' not in line: continue
                key,value=line.split('=',1)
                os.environ.setdefault(key.strip(),value.strip().strip('"').strip("'"))
            return True
        except FileNotFoundError: return False

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')

@dataclass(frozen=True)
class Settings:
    db_path: Path = ROOT / 'data' / 'trading.db'
    lock_path: Path = ROOT / 'data' / 'worker.lock'
    poll_seconds: int = int(os.getenv('POLL_SECONDS', '60'))
    starting_cash: float = float(os.getenv('STARTING_CASH', '10000'))
    max_risk_pct: float = float(os.getenv('MAX_RISK_PCT', '0.0025'))
    daily_loss_pct: float = float(os.getenv('DAILY_LOSS_PCT', '0.01'))
    symbol: str = os.getenv('BTC_SYMBOL', 'BTCUSDT')
    fee_rate: float = 0.001
    slippage_bps: float = 3.0
    max_positions: int = 2
    stale_seconds: int = 180
    # BTC does not trade in FX "pips"; these are basis-point scalp distances.
    # They are deliberately wider than the round-trip simulated fee.
    scalp_take_pct: float = float(os.getenv('SCALP_TAKE_PCT', '0.0035'))
    scalp_stop_pct: float = float(os.getenv('SCALP_STOP_PCT', '0.0025'))
    scalp_max_hold_minutes: int = int(os.getenv('SCALP_MAX_HOLD_MINUTES', '15'))
    trailing_stop_pct: float = float(os.getenv('TRAILING_STOP_PCT', '0.0015'))
    breakeven_trigger_pct: float = float(os.getenv('BREAKEVEN_TRIGGER_PCT', '0.0020'))
    # MT5 remains opt-in and read-only until a later, explicit demo-only
    # execution rollout. These values are intentionally not credentials.
    mt5_enabled: bool = os.getenv('MT5_ENABLED', '0') == '1'
    mt5_terminal_path: str = os.getenv('MT5_TERMINAL_PATH', '')
    mt5_symbol: str = os.getenv('MT5_SYMBOL', 'BTCUSD')
    mt5_demo_execution_enabled: bool = False

settings = Settings()
