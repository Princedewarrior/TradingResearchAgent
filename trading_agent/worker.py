from __future__ import annotations
import argparse, json, os, sys, time
from datetime import datetime, timezone
from .config import settings
from .db import initialize, connect, utcnow
from .data import BinancePublicProvider, DataError
from .strategy import btc_evaluation
from .engine import PaperEngine
from .shadow import record_evaluation, advance as advance_shadow

class SingleInstance:
    def __init__(self,path): self.path=path
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        try: self.fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY); os.write(self.fd,str(os.getpid()).encode())
        except FileExistsError: raise RuntimeError('worker already running or stale lock exists; use stop.ps1 after checking status')
        return self
    def __exit__(self,*_):
        os.close(self.fd)
        try: self.path.unlink()
        except FileNotFoundError: pass

def persist_candles(con,candles):
    for c in candles:
        con.execute('INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?,?,?,?,?,?,?)',(c.symbol,c.interval,c.open_time,c.close_time,c.open,c.high,c.low,c.close,c.volume,1,None))

def cycle(provider=None):
    initialize(settings.db_path,settings.starting_cash); provider=provider or BinancePublicProvider(); engine=PaperEngine(settings)
    with connect(settings.db_path) as con:
        con.execute("UPDATE worker_health SET status='running',updated_at=? WHERE id=1",(utcnow(),))
    try:
        candles=provider.candles(settings.symbol,'1m',200)
        with connect(settings.db_path) as con:
            started=time.perf_counter(); persist_candles(con,candles)
            exit_results=engine.process_exits(con,candles[-1]) + engine.process_short_exits(con,candles[-1])
            evaluation=btc_evaluation(candles,settings.scalp_stop_pct,settings.scalp_take_pct)
            # Shadow observations are deliberately isolated from PaperEngine:
            # no balance, position, order, or exchange state can be changed.
            advance_shadow(con,candles[-1],settings)
            record_evaluation(con,candles[-1],evaluation)
            short_enabled=bool(con.execute("SELECT enabled FROM strategies WHERE id='btc_short_paper'").fetchone()[0])
            action=evaluation['action']; reason=evaluation['reason']; strategy_id='btc_scalp'
            if action!='BUY' and evaluation['bearish']['passed']:
                if short_enabled:
                    action='SHORT'; reason='all short-entry checks passed'; strategy_id='btc_short_paper'
                else:
                    reason='bearish setup confirmed, but paper-short module is disabled pending sufficient backtest validation'
            order_key=f"{strategy_id}:{candles[-1].close_time}:{action}"
            evaluation['exit_checks']=exit_results
            evaluation['short_module']={'enabled':short_enabled,'reason':'enabled after validated backtest' if short_enabled else 'disabled pending sufficient backtest validation'}
            # A paused account must not even invoke an entry execution path.
            # Protective exits above deliberately remain active while paused.
            account_before_entry=engine._account(con)
            entries_paused=bool(account_before_entry['emergency_stop'])
            if action=='BUY' and entries_paused:
                status='paused: no autonomous paper entries'
            elif action=='SHORT' and entries_paused:
                status='paused: no autonomous paper entries'
            elif action=='BUY': status=engine.enter(con,'btc_scalp',order_key,candles[-1].close,evaluation['stop'],evaluation['take'],reason)
            elif action=='SHORT':
                account=engine._account(con); collateral=min(account['cash']*0.10, account['starting_cash']*0.10)
                status=engine.open_paper_short(con,'btc_short_paper',order_key,candles[-1].close,evaluation['short_stop'],evaluation['short_take'],collateral,reason)
            else: status='recorded'
            order=con.execute('SELECT quantity,fill_price,fee,reason FROM orders WHERE decision_key=?',(order_key,)).fetchone()
            account=engine._account(con)
            evaluation['execution']={
                'action':action,'status':status,'order_key':order_key,
                'new_entries_paused':bool(account['emergency_stop']),
                'position_size':order['quantity'] if order else 0,
                'fill_price':order['fill_price'] if order else None,
                'entry_fee':order['fee'] if order else 0,
                'reason':order['reason'] if order else reason,
            }
            duration=(time.perf_counter()-started)*1000
            decision_key=f"evaluation:btc_scalp:{utcnow()}"
            con.execute('INSERT INTO decisions(idempotency_key,strategy_id,created_at,action,reason,price,status,market_time,evaluation_json,duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?)',(decision_key,strategy_id,utcnow(),action,reason,candles[-1].close,status,evaluation['market_time'],json.dumps(evaluation,sort_keys=True),duration))
            engine.record_snapshot(con,candles[-1].close)
            con.execute("UPDATE worker_health SET status='running',last_success=?,last_error=NULL,updated_at=? WHERE id=1",(utcnow(),utcnow()))
    except Exception as e:
        with connect(settings.db_path) as con:
            # One persistent outage should create one attention item, not a new
            # notification every polling cycle. Record it again only after 15m.
            last=con.execute("SELECT message,created_at FROM quality_events WHERE source='worker' ORDER BY id DESC LIMIT 1").fetchone()
            cutoff=datetime.now(timezone.utc).timestamp()-900
            last_time=datetime.fromisoformat(last['created_at']).timestamp() if last else 0
            if not last or last['message'] != str(e) or last_time < cutoff:
                con.execute('INSERT INTO quality_events(created_at,source,message) VALUES(?,?,?)',(utcnow(),'worker',str(e)))
            con.execute("UPDATE worker_health SET status='degraded',last_error=?,updated_at=? WHERE id=1",(str(e),utcnow()))
        raise

def main():
    parser=argparse.ArgumentParser(description='Paper-only worker; never sends exchange orders.')
    parser.add_argument('--once',action='store_true'); args=parser.parse_args()
    initialize(settings.db_path,settings.starting_cash)
    with SingleInstance(settings.lock_path):
        while True:
            try: cycle()
            except Exception as e: print(f'{utcnow()} cycle failed: {e}',file=sys.stderr)
            if args.once: break
            time.sleep(settings.poll_seconds)

if __name__=='__main__': main()
