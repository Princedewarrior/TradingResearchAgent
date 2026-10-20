from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from trading_agent.data import Candle, DataError, validate_candles
from trading_agent.engine import size_for_risk, PaperEngine
from trading_agent.db import initialize, connect
from trading_agent.config import Settings
from trading_agent.backtest import run_btc_scalp_backtest
from trading_agent.reconciliation import repair_missing_ledger_events, report
from trading_agent.shadow import record_evaluation, advance as advance_shadow, summary as shadow_summary

def candle(i=0, low=99, high=102, close=101):
    now=datetime.now(timezone.utc)-timedelta(minutes=10-i)
    return Candle('BTCUSDT','1m',now.isoformat(),(now+timedelta(minutes=1)).isoformat(),100,high,low,close,1)

def test_ohlc_validation_rejects_impossible():
    with pytest.raises(DataError): validate_candles([candle(low=103,high=102)])

def test_duplicate_candles_rejected():
    c=candle()
    with pytest.raises(DataError): validate_candles([c,c])

def test_incomplete_candle_removed():
    future=datetime.now(timezone.utc)+timedelta(minutes=2)
    c=Candle('BTCUSDT','1m',(future-timedelta(minutes=1)).isoformat(),future.isoformat(),100,101,99,100,1)
    with pytest.raises(DataError): validate_candles([c])

def test_size_respects_stop_distance():
    assert size_for_risk(10000,100,98,.0025,.001) > 0
    assert size_for_risk(10000,100,100,.0025,.001) == 0

def test_entry_and_conservative_stop(tmp_path):
    s=Settings(db_path=tmp_path/'a.db',lock_path=tmp_path/'x.lock',starting_cash=10000)
    initialize(s.db_path,s.starting_cash); engine=PaperEngine(s)
    with connect(s.db_path) as con:
        assert engine.enter(con,'btc_scalp','k1',100,98,104,'test')=='filled'
        engine.process_exits(con,candle(low=97,high=105,close=100))
        p=con.execute('select * from positions').fetchone()
        assert p['status']=='closed' and p['exit_reason']=='stop_loss'

def test_emergency_stop_blocks_entries(tmp_path):
    s=Settings(db_path=tmp_path/'a.db',lock_path=tmp_path/'x.lock')
    initialize(s.db_path,s.starting_cash)
    with connect(s.db_path) as con:
        con.execute("update accounts set emergency_stop=1 where id='btc-paper'")
        assert PaperEngine(s).enter(con,'btc_scalp','k',100,98,104,'x').startswith('rejected')

def test_pause_keeps_protective_exits_active(tmp_path):
    s=Settings(db_path=tmp_path/'a.db',lock_path=tmp_path/'x.lock')
    initialize(s.db_path,s.starting_cash)
    with connect(s.db_path) as con:
        engine=PaperEngine(s); assert engine.enter(con,'btc_scalp','k',100,98,104,'x')=='filled'
        con.execute("update accounts set emergency_stop=1 where id='btc-paper'")
        result=engine.process_exits(con,candle(low=97,high=100,close=98))
        assert result[0]['result']=='stop_loss'
        assert con.execute("select status from positions where id=?",(result[0]['position_id'],)).fetchone()[0]=='closed'

def test_pause_persists_through_initialization_restart(tmp_path):
    s=Settings(db_path=tmp_path/'a.db',lock_path=tmp_path/'x.lock')
    initialize(s.db_path,s.starting_cash)
    with connect(s.db_path) as con:
        con.execute("update accounts set emergency_stop=1 where id='btc-paper'")
    initialize(s.db_path,s.starting_cash)
    with connect(s.db_path) as con:
        assert con.execute("select emergency_stop from accounts where id='btc-paper'").fetchone()[0]==1

def test_reconciliation_repairs_missing_source_backed_ledger_once(tmp_path):
    s=Settings(db_path=tmp_path/'a.db',lock_path=tmp_path/'x.lock',starting_cash=10000)
    initialize(s.db_path,s.starting_cash)
    with connect(s.db_path) as con:
        engine=PaperEngine(s); assert engine.enter(con,'btc_scalp','source',100,98,104,'source')=='filled'
        engine.process_exits(con,candle(low=97,high=100,close=98))
        con.execute('delete from paper_ledger')
    first=repair_missing_ledger_events(s.db_path); second=repair_missing_ledger_events(s.db_path)
    assert len(first)==1 and not second
    data=report(s.db_path)
    assert data['missing_repairable_events']==0

def test_shadow_observations_never_touch_orders_or_account_and_dedupe(tmp_path):
    s=Settings(db_path=tmp_path/'a.db',lock_path=tmp_path/'x.lock',starting_cash=10000)
    initialize(s.db_path,s.starting_cash)
    first=Candle('BTCUSDT','1m','2025-01-01T00:00:00+00:00','2025-01-01T00:01:00+00:00',100,100,100,100,1)
    evaluation={'regime':'bullish','reason':'qualified','long':{'passed':True,'failed':[]},'bearish':{'passed':True,'failed':[]},'stop':98,'take':102,'short_stop':102,'short_take':98}
    with connect(s.db_path) as con:
        before=dict(con.execute("select cash,btc from accounts where id='btc-paper'").fetchone())
        record_evaluation(con,first,evaluation); record_evaluation(con,first,evaluation)
        assert con.execute('select count(*) from shadow_observations').fetchone()[0]==2
        assert con.execute('select count(*) from orders').fetchone()[0]==0
        second=Candle('BTCUSDT','1m','2025-01-01T00:01:00+00:00','2025-01-01T00:02:00+00:00',100,103,97,100,1)
        advance_shadow(con,second,s)
        after=dict(con.execute("select cash,btc from accounts where id='btc-paper'").fetchone())
        assert after==before
        closed=con.execute("select direction,net_pnl,status from shadow_observations order by direction").fetchall()
        assert {x['direction'] for x in closed}=={'LONG','SHORT'} and all(x['status']=='CLOSED' for x in closed)
        assert all(x['net_pnl'] is not None for x in closed)
        assert shadow_summary(con)['closed']==2

def test_shadow_summary_includes_freshness_and_expiry(tmp_path):
    s=Settings(db_path=tmp_path/'a.db',lock_path=tmp_path/'x.lock',starting_cash=10000)
    initialize(s.db_path,s.starting_cash)
    first=Candle('BTCUSDT','1m','2025-01-01T00:00:00+00:00','2025-01-01T00:01:00+00:00',100,100,100,100,1)
    evaluation={'regime':'range','reason':'qualified','long':{'passed':True,'failed':[]},'bearish':{'passed':False,'failed':[]},'stop':50,'take':150,'short_stop':102,'short_take':98}
    with connect(s.db_path) as con:
        record_evaluation(con,first,evaluation)
        later=Candle('BTCUSDT','1m','2025-01-01T00:16:00+00:00','2025-01-01T00:17:00+00:00',100,101,99,101,1)
        advance_shadow(con,later,s)
        expiry=Candle('BTCUSDT','1m','2025-01-01T00:32:00+00:00','2025-01-01T00:33:00+00:00',101,101,99,101,1)
        advance_shadow(con,expiry,s)
        report=shadow_summary(con)
        assert report['latest_evaluation']['price']==100
        assert report['closed']==1
        assert con.execute("select status from shadow_observations").fetchone()[0]=='EXPIRED'

def test_separate_strategy_records(tmp_path):
    s=Settings(db_path=tmp_path/'a.db',lock_path=tmp_path/'x.lock'); initialize(s.db_path,s.starting_cash)
    with connect(s.db_path) as con:
        rows=con.execute('select id from strategies').fetchall()
        assert {x['id'] for x in rows}=={'btc_scalp','btc_swing','btc_short_paper','eurusd_scalp','eurusd_swing'}

def test_recovery_does_not_duplicate_order(tmp_path):
    s=Settings(db_path=tmp_path/'a.db',lock_path=tmp_path/'x.lock'); initialize(s.db_path,s.starting_cash)
    with connect(s.db_path) as con:
        e=PaperEngine(s); assert e.enter(con,'btc_scalp','same',100,98,104,'x')=='filled'
        assert e.enter(con,'btc_scalp','same',100,98,104,'x')=='already processed'
        assert con.execute('select count(*) from orders').fetchone()[0]==1

def test_backtest_uses_next_candle_and_costs(tmp_path):
    s=Settings(db_path=tmp_path/'a.db',lock_path=tmp_path/'x.lock',starting_cash=10000)
    start=datetime(2025,1,1,tzinfo=timezone.utc)
    candles=[]
    prices=[100.0]*18
    for _ in range(8): prices.append(prices[-1]-.2)
    for _ in range(7): prices.append(prices[-1]+.3)
    for i,price in enumerate(prices):
        t=start+timedelta(minutes=i)
        candles.append(Candle('BTCUSDT','1m',t.isoformat(),(t+timedelta(minutes=1)).isoformat(),price,price+.02,price-.02,price+.05,1))
    result=run_btc_scalp_backtest(candles,s)
    assert result.trade_count >= 1
    assert result.fees_paid > 0
    assert result.slippage_cost > 0
