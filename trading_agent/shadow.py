"""Isolated, paper-free shadow signal monitoring.

Signals are created at completed candle close, filled only at the next
completed candle's open, and never invoke PaperEngine or exchange APIs.
"""
from __future__ import annotations
import json
import uuid
from datetime import datetime
from .db import utcnow

HORIZON_MINUTES=15

def _price_with_slippage(price, direction, entering, settings):
    # Long: buy high / sell low. Short: sell low / cover high.
    adverse = 1 + settings.slippage_bps/10000 if (direction=='LONG')==entering else 1-settings.slippage_bps/10000
    return price*adverse

def record_evaluation(con, candle, evaluation, stale=False):
    key=f"shadow-eval:{candle.symbol}:{candle.interval}:{candle.close_time}"
    long=evaluation['long']; short=evaluation['bearish']
    con.execute('''INSERT OR IGNORE INTO shadow_evaluations(idempotency_key,created_at,market_time,symbol,timeframe,regime,price,long_json,short_json,hold_reason,stale)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(key,utcnow(),candle.close_time,candle.symbol,candle.interval,evaluation['regime'],candle.close,json.dumps(long,sort_keys=True),json.dumps(short,sort_keys=True),evaluation['reason'],int(stale)))
    candidates=[]
    if long['passed']: candidates.append(('LONG','shadow_long',evaluation['stop'],evaluation['take']))
    if short['passed']: candidates.append(('SHORT','shadow_short',evaluation['short_stop'],evaluation['short_take']))
    for direction,strategy,stop,take in candidates:
        signal_key=f"shadow:{strategy}:{candle.close_time}:{direction}"
        con.execute('''INSERT OR IGNORE INTO shadow_observations(id,signal_key,strategy_id,direction,symbol,timeframe,signal_time,signal_price,stop_price,take_price,horizon_minutes,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(str(uuid.uuid4()),signal_key,strategy,direction,candle.symbol,candle.interval,candle.close_time,candle.close,stop,take,HORIZON_MINUTES,'PENDING',utcnow(),utcnow()))

def advance(con, candle, settings):
    """Fill prior signals then update only existing virtual observations."""
    results=[]
    for o in con.execute("SELECT * FROM shadow_observations WHERE status='PENDING' AND signal_time<?",(candle.close_time,)).fetchall():
        fill=_price_with_slippage(candle.open,o['direction'],True,settings); fee=fill*settings.fee_rate
        con.execute("UPDATE shadow_observations SET status='OPEN',opened_at=?,entry_price=?,entry_fee=?,updated_at=? WHERE id=?",(candle.open_time,fill,fee,utcnow(),o['id']))
        results.append({'id':o['id'],'event':'opened'})
    for o in con.execute("SELECT * FROM shadow_observations WHERE status='OPEN'").fetchall():
        entry=o['entry_price']; direction=o['direction']
        favorable=(candle.high-entry) if direction=='LONG' else (entry-candle.low)
        adverse=(entry-candle.low) if direction=='LONG' else (candle.high-entry)
        mfe=max(o['mfe'],favorable); mae=max(o['mae'],adverse)
        hit_stop=candle.low<=o['stop_price'] if direction=='LONG' else candle.high>=o['stop_price']
        hit_take=candle.high>=o['take_price'] if direction=='LONG' else candle.low<=o['take_price']
        age=(datetime.fromisoformat(candle.close_time)-datetime.fromisoformat(o['opened_at'])).total_seconds()/60
        if hit_stop: raw,reason=o['stop_price'],'stop'
        elif hit_take: raw,reason=o['take_price'],'target'
        elif age>=o['horizon_minutes']: raw,reason=candle.close,'expired'
        else:
            con.execute('UPDATE shadow_observations SET mfe=?,mae=?,updated_at=? WHERE id=?',(mfe,mae,utcnow(),o['id'])); continue
        fill=_price_with_slippage(raw,direction,False,settings); exit_fee=fill*settings.fee_rate
        gross=((fill-entry) if direction=='LONG' else (entry-fill))*o['quantity']; net=gross-o['entry_fee']-exit_fee
        status='EXPIRED' if reason=='expired' else 'CLOSED'
        con.execute("UPDATE shadow_observations SET status=?,mfe=?,mae=?,exit_price=?,closed_at=?,exit_fee=?,gross_pnl=?,net_pnl=?,exit_reason=?,updated_at=? WHERE id=?",(status,mfe,mae,fill,candle.close_time,exit_fee,gross,net,reason,utcnow(),o['id']))
        results.append({'id':o['id'],'event':reason})
    return results

def summary(con):
    observations=[dict(x) for x in con.execute('SELECT * FROM shadow_observations ORDER BY signal_time DESC').fetchall()]
    finalized=[x for x in observations if x['status'] in ('CLOSED','EXPIRED','INVALIDATED')]
    wins=[x['net_pnl'] for x in finalized if (x['net_pnl'] or 0)>0]
    losses=[x['net_pnl'] for x in finalized if (x['net_pnl'] or 0)<=0]
    by_direction={}
    for direction in ('LONG','SHORT'):
        group=[x for x in finalized if x['direction']==direction]
        by_direction[direction]={'closed':len(group),'net_pnl':sum((x['net_pnl'] or 0) for x in group),'fees':sum(x['entry_fee']+x['exit_fee'] for x in group)}
    latest=con.execute('SELECT market_time,price,stale FROM shadow_evaluations ORDER BY id DESC LIMIT 1').fetchone()
    health=con.execute("SELECT status,last_success,last_error FROM worker_health WHERE id=1").fetchone()
    failures=con.execute("SELECT count(*) FROM quality_events WHERE source='worker'").fetchone()[0]
    grouped={}
    for x in finalized:
        key=f"{x['strategy_id']}|{x['direction']}|{x['horizon_minutes']}m"
        grouped.setdefault(key,{'count':0,'net_pnl':0.0})
        grouped[key]['count']+=1; grouped[key]['net_pnl']+=x['net_pnl'] or 0
    bounds=con.execute("SELECT min(open_time),max(open_time),count(*) FROM candles WHERE symbol='BTCUSDT' AND interval='1m'").fetchone()
    coverage={'candles':bounds[2],'missing_candles':None}
    if bounds[0] and bounds[1]:
        expected=round((datetime.fromisoformat(bounds[1])-datetime.fromisoformat(bounds[0])).total_seconds()/60)+1
        coverage['missing_candles']=max(0,expected-bounds[2])
    return {'monitoring':'ACTIVE - HYPOTHETICAL ONLY','open':sum(x['status']=='OPEN' for x in observations),'pending':sum(x['status']=='PENDING' for x in observations),'closed':len(finalized),'net_pnl':sum((x['net_pnl'] or 0) for x in finalized),'fees':sum(x['entry_fee']+x['exit_fee'] for x in finalized),'win_rate':(len(wins)/len(finalized)*100 if finalized else None),'profit_factor':(sum(wins)/-sum(losses) if losses else None),'by_direction':by_direction,'by_group':grouped,'data_coverage':coverage,'latest_evaluation':dict(latest) if latest else None,'worker_health':dict(health) if health else None,'connection_failures':failures,'observations':observations[:25]}
