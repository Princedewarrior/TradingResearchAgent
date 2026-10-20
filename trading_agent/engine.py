from __future__ import annotations
import uuid
from datetime import datetime, timezone
from .db import connect, utcnow

def size_for_risk(equity: float, entry: float, stop: float, risk_pct: float, fee_rate: float) -> float:
    risk=max(entry-stop,0)
    if risk<=0 or entry<=0: return 0
    return max(0,(equity*risk_pct)/(risk + entry*fee_rate))

class PaperEngine:
    def __init__(self, settings): self.s=settings
    def _account(self, con): return con.execute("SELECT * FROM accounts WHERE id='btc-paper'").fetchone()
    def equity(self, account, price): return account['cash']+account['btc']*price
    def record_snapshot(self, con, price):
        a=self._account(con); con.execute('INSERT INTO snapshots(created_at,cash,btc,mark_price,equity,realized_pnl) VALUES(?,?,?,?,?,COALESCE((SELECT SUM(realized_pnl) FROM positions WHERE status=\'closed\'),0))',(utcnow(),a['cash'],a['btc'],price,self.equity(a,price)))
    def process_exits(self, con, candle):
        results=[]
        for p in con.execute("SELECT * FROM positions WHERE status='open'").fetchall():
            opened=datetime.fromisoformat(p['opened_at'])
            candle_time=datetime.fromisoformat(candle.close_time)
            timed_out=(candle_time-opened).total_seconds() >= self.s.scalp_max_hold_minutes*60
            # A bar's high cannot tighten a stop before its low is evaluated:
            # OHLC data does not reveal intrabar order. Tightening after a
            # surviving bar makes the protection effective from the next bar.
            hit_stop=candle.low<=p['stop_price']; hit_take=candle.high>=p['take_price']
            if not (hit_stop or hit_take or timed_out):
                peak=max(p['peak_price'] or p['entry_price'],candle.high)
                protective=max(p['stop_price'],peak*(1-self.s.trailing_stop_pct))
                if peak >= p['entry_price']*(1+self.s.breakeven_trigger_pct):
                    protective=max(protective,p['entry_price']*(1+self.s.fee_rate*2))
                if protective > p['stop_price']:
                    con.execute('UPDATE positions SET peak_price=?,trailing_stop=?,stop_price=? WHERE id=?',(peak,protective,protective,p['id']))
                    p=con.execute('SELECT * FROM positions WHERE id=?',(p['id'],)).fetchone()
                results.append({'position_id':p['id'],'result':'held','stop':p['stop_price'],'target':p['take_price']})
                continue
            # Conservative when both bounds occur within an OHLC candle.
            if hit_stop: price,reason=p['stop_price'],'stop_loss'
            elif hit_take: price,reason=p['take_price'],'take_profit'
            else: price,reason=candle.close,'scalp_time_exit'
            qty=p['quantity']; fill=price*(1-self.s.slippage_bps/10000); fee=qty*fill*self.s.fee_rate
            a=self._account(con); pnl=qty*(fill-p['entry_price'])-p['fees']-fee
            con.execute('UPDATE accounts SET cash=?, btc=?, updated_at=? WHERE id=\'btc-paper\'',(a['cash']+qty*fill-fee,a['btc']-qty,utcnow()))
            con.execute('UPDATE positions SET status=\'closed\',closed_at=?,exit_price=?,realized_pnl=?,fees=fees+?,exit_reason=? WHERE id=?',(utcnow(),fill,pnl,fee,reason,p['id']))
            con.execute('INSERT INTO paper_ledger(created_at,side,quantity,price,fee,note,order_id) VALUES(?,?,?,?,?,?,?)',(utcnow(),'SELL',qty,fill,fee,reason,None))
            results.append({'position_id':p['id'],'result':reason,'fill_price':fill})
        return results
    def enter(self, con, strategy_id, key, price, stop, take, reason):
        # Recovery/retry can revisit a completed candle. The order key is a
        # durable idempotency boundary, rather than merely a best-effort check.
        if con.execute('SELECT 1 FROM orders WHERE decision_key=?',(key,)).fetchone():
            return 'already processed'
        a=self._account(con)
        if a['emergency_stop']: return 'rejected: emergency stop'
        open_count=con.execute("SELECT count(*) FROM positions WHERE status='open'").fetchone()[0]
        if open_count>=self.s.max_positions: return 'rejected: max positions'
        equity=self.equity(a,price)
        daily=con.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM positions WHERE status='closed' AND closed_at>=?",(datetime.now(timezone.utc).date().isoformat(),)).fetchone()[0]
        if daily <= -a['starting_cash']*self.s.daily_loss_pct: return 'rejected: daily loss lockout'
        qty=size_for_risk(equity,price,stop,self.s.max_risk_pct,self.s.fee_rate)
        fill=price*(1+self.s.slippage_bps/10000); fee=qty*fill*self.s.fee_rate; cost=qty*fill+fee
        if qty<=0 or cost>a['cash']: return 'rejected: insufficient balance or invalid stop'
        order_id=str(uuid.uuid4()); pos_id=str(uuid.uuid4())
        con.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?)',(order_id,key,utcnow(),'BUY',self.s.symbol,qty,price,fill,fee,'filled',reason))
        con.execute('UPDATE accounts SET cash=?,btc=?,updated_at=? WHERE id=\'btc-paper\'',(a['cash']-cost,a['btc']+qty,utcnow()))
        con.execute('INSERT INTO positions(id,strategy_id,symbol,opened_at,quantity,entry_price,stop_price,take_price,fees,status) VALUES(?,?,?,?,?,?,?,?,?,\'open\')',(pos_id,strategy_id,self.s.symbol,utcnow(),qty,fill,stop,take,fee))
        con.execute('INSERT INTO paper_ledger(created_at,side,quantity,price,fee,note,order_id) VALUES(?,?,?,?,?,?,?)',(utcnow(),'BUY',qty,fill,fee,reason,order_id))
        return 'filled'

    def manual_sell(self, con, key, quantity, price, reason='manual paper sell'):
        if quantity<=0: return 'rejected: quantity must be positive'
        if con.execute('SELECT 1 FROM orders WHERE decision_key=?',(key,)).fetchone(): return 'already processed'
        a=self._account(con)
        if quantity>a['btc']+1e-12: return 'rejected: insufficient simulated Bitcoin'
        fill=price*(1-self.s.slippage_bps/10000); fee=quantity*fill*self.s.fee_rate
        order_id=str(uuid.uuid4())
        con.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?)',(order_id,key,utcnow(),'SELL',self.s.symbol,quantity,price,fill,fee,'filled',reason))
        con.execute('UPDATE accounts SET cash=?,btc=?,updated_at=? WHERE id=\'btc-paper\'',(a['cash']+quantity*fill-fee,max(0,a['btc']-quantity),utcnow()))
        remaining=quantity
        for p in con.execute("SELECT * FROM positions WHERE status='open' ORDER BY opened_at").fetchall():
            if remaining<=0: break
            q=min(remaining,p['quantity']); pnl=q*(fill-p['entry_price'])-(p['fees']*(q/p['quantity']))-fee*(q/quantity)
            if q>=p['quantity']-1e-12:
                con.execute("UPDATE positions SET status='closed',closed_at=?,exit_price=?,realized_pnl=?,fees=fees+?,exit_reason='manual_sell' WHERE id=?",(utcnow(),fill,pnl,fee*(q/quantity),p['id']))
            else:
                # Materialize the sold slice as its own closed position. This
                # keeps realized P/L and fees reconcilable without treating a
                # normal SELL as a short or losing the partial-sale result.
                allocated_entry_fee=p['fees']*(q/p['quantity'])
                slice_id='partial-'+str(uuid.uuid4())
                con.execute('INSERT INTO positions(id,strategy_id,symbol,opened_at,closed_at,quantity,entry_price,stop_price,take_price,exit_price,realized_pnl,fees,exit_reason,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(slice_id,p['strategy_id'],p['symbol'],p['opened_at'],utcnow(),q,p['entry_price'],p['stop_price'],p['take_price'],fill,pnl,allocated_entry_fee+fee*(q/quantity),'manual_partial_sell','closed'))
                con.execute('UPDATE positions SET quantity=quantity-?, fees=fees-? WHERE id=?',(q,allocated_entry_fee,p['id']))
            remaining-=q
        con.execute('INSERT INTO paper_ledger(created_at,side,quantity,price,fee,note,order_id) VALUES(?,?,?,?,?,?,?)',(utcnow(),'SELL',quantity,fill,fee,reason,order_id))
        return 'filled'

    def close_position(self, con, position_id, price, reason='manual position close'):
        """Close exactly one named paper position; never affects Binance."""
        p=con.execute("SELECT * FROM positions WHERE id=? AND status='open'",(position_id,)).fetchone()
        if not p: return 'rejected: paper position is not open'
        key='close-position:'+position_id
        if con.execute('SELECT 1 FROM orders WHERE decision_key=?',(key,)).fetchone(): return 'already processed'
        a=self._account(con); qty=p['quantity']
        fill=price*(1-self.s.slippage_bps/10000); fee=qty*fill*self.s.fee_rate
        pnl=qty*(fill-p['entry_price'])-p['fees']-fee; order_id=str(uuid.uuid4())
        con.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?)',(order_id,key,utcnow(),'SELL',self.s.symbol,qty,price,fill,fee,'filled',reason))
        con.execute('UPDATE accounts SET cash=?,btc=?,updated_at=? WHERE id=\'btc-paper\'',(a['cash']+qty*fill-fee,max(0,a['btc']-qty),utcnow()))
        con.execute("UPDATE positions SET status='closed',closed_at=?,exit_price=?,realized_pnl=?,fees=fees+?,exit_reason=? WHERE id=?",(utcnow(),fill,pnl,fee,reason,position_id))
        con.execute('INSERT INTO paper_ledger(created_at,side,quantity,price,fee,note,order_id) VALUES(?,?,?,?,?,?,?)',(utcnow(),'SELL',qty,fill,fee,reason,order_id))
        return 'filled'

    # This isolated simulator intentionally has no call path to Binance and is
    # not enabled by the worker. Collateral is cash reserved 1:1 against the
    # short notional: no leverage, borrowing, or funding is simulated.
    def open_paper_short(self, con, strategy_id, key, price, stop, take, collateral, reason):
        if con.execute('SELECT 1 FROM orders WHERE decision_key=?',(key,)).fetchone(): return 'already processed'
        account=self._account(con)
        if account['emergency_stop']: return 'rejected: emergency stop'
        if collateral<=0 or collateral>account['cash'] or stop<=price or take>=price: return 'rejected: invalid short risk or insufficient collateral'
        qty=collateral/price; fee=qty*price*self.s.fee_rate; total=collateral+fee
        if total>account['cash']: return 'rejected: insufficient collateral for fee'
        order_id=str(uuid.uuid4()); position_id=str(uuid.uuid4()); fill=price*(1-self.s.slippage_bps/10000)
        con.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?)',(order_id,key,utcnow(),'SHORT',self.s.symbol,qty,price,fill,fee,'filled',reason))
        con.execute("UPDATE accounts SET cash=cash-?,updated_at=? WHERE id='btc-paper'",(total,utcnow()))
        con.execute('INSERT INTO paper_short_positions(id,strategy_id,symbol,opened_at,quantity,entry_price,stop_price,take_price,collateral,entry_fee,status) VALUES(?,?,?,?,?,?,?,?,?,?,\'open\')',(position_id,strategy_id,self.s.symbol,utcnow(),qty,fill,stop,take,collateral,fee))
        con.execute('INSERT INTO paper_ledger(created_at,side,quantity,price,fee,note,order_id) VALUES(?,?,?,?,?,?,?)',(utcnow(),'SHORT',qty,fill,fee,reason,order_id))
        return 'filled'

    def process_short_exits(self, con, candle):
        results=[]
        for p in con.execute("SELECT * FROM paper_short_positions WHERE status='open'").fetchall():
            hit_stop=candle.high>=p['stop_price']; hit_take=candle.low<=p['take_price']
            if not (hit_stop or hit_take):
                results.append({'position_id':p['id'],'result':'held'}); continue
            exit_price=p['stop_price'] if hit_stop else p['take_price']; reason='short_stop_loss' if hit_stop else 'short_take_profit'
            fill=exit_price*(1+self.s.slippage_bps/10000); fee=p['quantity']*fill*self.s.fee_rate
            pnl=p['quantity']*(p['entry_price']-fill)-p['entry_fee']-fee
            # Losses cannot silently create credit: any deficit remains a loss
            # to paper equity, while cash is floored at zero.
            returned=max(0,p['collateral']+pnl)
            con.execute("UPDATE accounts SET cash=cash+?,updated_at=? WHERE id='btc-paper'",(returned,utcnow()))
            con.execute("UPDATE paper_short_positions SET status='closed',closed_at=?,exit_price=?,exit_fee=?,realized_pnl=?,exit_reason=? WHERE id=?",(utcnow(),fill,fee,pnl,reason,p['id']))
            con.execute('INSERT INTO paper_ledger(created_at,side,quantity,price,fee,note,order_id) VALUES(?,?,?,?,?,?,?)',(utcnow(),'COVER',p['quantity'],fill,fee,reason,None))
            results.append({'position_id':p['id'],'result':reason,'fill_price':fill})
        return results
