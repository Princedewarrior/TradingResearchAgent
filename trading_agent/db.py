from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone

SCHEMA = '''
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS candles(symbol TEXT NOT NULL, interval TEXT NOT NULL, open_time TEXT NOT NULL, close_time TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, valid INTEGER NOT NULL, raw_json TEXT, PRIMARY KEY(symbol,interval,open_time));
CREATE TABLE IF NOT EXISTS quality_events(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, source TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, cash REAL NOT NULL, btc REAL NOT NULL, starting_cash REAL NOT NULL, emergency_stop INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS strategies(id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, market TEXT NOT NULL, timeframe TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT UNIQUE NOT NULL, strategy_id TEXT NOT NULL, created_at TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, price REAL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, decision_key TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, side TEXT NOT NULL, symbol TEXT NOT NULL, quantity REAL NOT NULL, requested_price REAL NOT NULL, fill_price REAL, fee REAL, status TEXT NOT NULL, reason TEXT);
CREATE TABLE IF NOT EXISTS positions(id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, symbol TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT, quantity REAL NOT NULL, entry_price REAL NOT NULL, stop_price REAL NOT NULL, take_price REAL NOT NULL, exit_price REAL, realized_pnl REAL, fees REAL NOT NULL DEFAULT 0, exit_reason TEXT, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, cash REAL NOT NULL, btc REAL NOT NULL, mark_price REAL, equity REAL, realized_pnl REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS worker_health(id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL, last_success TEXT, last_error TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, side TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL, note TEXT NOT NULL, order_id TEXT);
CREATE TABLE IF NOT EXISTS live_wallet_sync(id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL, last_success TEXT, last_error TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS live_wallet_balances(asset TEXT PRIMARY KEY, free REAL NOT NULL, locked REAL NOT NULL, synced_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_messages(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, role TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_short_positions(id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, symbol TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT, quantity REAL NOT NULL, entry_price REAL NOT NULL, stop_price REAL NOT NULL, take_price REAL NOT NULL, collateral REAL NOT NULL, entry_fee REAL NOT NULL DEFAULT 0, exit_fee REAL NOT NULL DEFAULT 0, exit_price REAL, realized_pnl REAL, exit_reason TEXT, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_evaluations(id INTEGER PRIMARY KEY AUTOINCREMENT, idempotency_key TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, market_time TEXT NOT NULL, symbol TEXT NOT NULL, timeframe TEXT NOT NULL, regime TEXT NOT NULL, price REAL NOT NULL, long_json TEXT NOT NULL, short_json TEXT NOT NULL, hold_reason TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS shadow_observations(id TEXT PRIMARY KEY, signal_key TEXT UNIQUE NOT NULL, strategy_id TEXT NOT NULL, direction TEXT NOT NULL CHECK(direction IN ('LONG','SHORT')), symbol TEXT NOT NULL, timeframe TEXT NOT NULL, signal_time TEXT NOT NULL, signal_price REAL NOT NULL, opened_at TEXT, entry_price REAL, quantity REAL NOT NULL DEFAULT 1.0, stop_price REAL, take_price REAL, horizon_minutes INTEGER NOT NULL, entry_fee REAL NOT NULL DEFAULT 0, exit_fee REAL NOT NULL DEFAULT 0, mfe REAL NOT NULL DEFAULT 0, mae REAL NOT NULL DEFAULT 0, exit_price REAL, closed_at TEXT, gross_pnl REAL, net_pnl REAL, exit_reason TEXT, status TEXT NOT NULL CHECK(status IN ('PENDING','OPEN','CLOSED','INVALIDATED','EXPIRED')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_shadow_observations_status ON shadow_observations(status,signal_time);
INSERT OR IGNORE INTO schema_version(version) VALUES(1);
'''

def utcnow() -> str: return datetime.now(timezone.utc).isoformat()

@contextmanager
def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA foreign_keys=ON'); yield con; con.commit()
    except Exception:
        con.rollback(); raise
    finally: con.close()

def initialize(path: Path, starting_cash: float):
    with connect(path) as con:
        con.executescript(SCHEMA)
        # Backward-compatible migration for profit-protection state.
        columns={r['name'] for r in con.execute('PRAGMA table_info(positions)').fetchall()}
        for name in ('peak_price','trailing_stop'):
            if name not in columns: con.execute(f'ALTER TABLE positions ADD COLUMN {name} REAL')
        decision_columns={r['name'] for r in con.execute('PRAGMA table_info(decisions)').fetchall()}
        for name, kind in (('market_time','TEXT'),('evaluation_json','TEXT'),('duration_ms','REAL')):
            if name not in decision_columns: con.execute(f'ALTER TABLE decisions ADD COLUMN {name} {kind}')
        now=utcnow()
        con.execute("INSERT OR IGNORE INTO accounts VALUES ('btc-paper',?,?,?,0,?)", (starting_cash,0.0,starting_cash,now))
        for sid, market, timeframe in [('btc_scalp','BTC/USDT','1m/5m'),('btc_swing','BTC/USDT','1h/4h'),('eurusd_scalp','EUR/USD','1m/5m'),('eurusd_swing','EUR/USD','1h/4h')]:
            con.execute('INSERT OR IGNORE INTO strategies VALUES (?,?,?,?,?)',(sid, 1 if market=='BTC/USDT' else 0,market,timeframe,now))
        # The separate short simulator is deliberately disabled until its
        # accounting is enabled and tested; it never maps to a Binance order.
        con.execute('INSERT OR IGNORE INTO strategies VALUES (?,?,?,?,?)',('btc_short_paper',0,'BTC/USDT','1m/5m',now))
        # Initialization is called by the dashboard too; it must never make a
        # healthy independent worker appear stopped.
        con.execute("INSERT OR IGNORE INTO worker_health(id,status,updated_at) VALUES(1,'stopped',?)",(now,))
        con.execute("INSERT OR IGNORE INTO live_wallet_sync(id,status,updated_at) VALUES(1,'not configured',?)",(now,))
