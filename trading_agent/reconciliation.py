"""Paper-account audit and idempotent historical-ledger repair tools."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import settings
from .db import connect, initialize


def _matching_entry_order(con, position):
    return con.execute(
        """SELECT * FROM orders WHERE side='BUY' AND symbol=?
           AND ABS(quantity-?) < 0.0000000001 AND ABS(fill_price-?) < 0.0001
           ORDER BY created_at LIMIT 1""",
        (position['symbol'], position['quantity'], position['entry_price']),
    ).fetchone()


def missing_ledger_events(con) -> list[dict]:
    """Find reconstructable long events without guessing from price movement.

    Only records where the position, its filled entry order, and its completed
    exit fields agree are eligible. Manual partial-sale positions are skipped.
    """
    missing=[]
    positions=con.execute("SELECT * FROM positions WHERE status='closed' ORDER BY opened_at").fetchall()
    for position in positions:
        entry=_matching_entry_order(con, position)
        if not entry:
            continue
        has_entry=con.execute('SELECT 1 FROM paper_ledger WHERE order_id=?',(entry['id'],)).fetchone()
        has_exit=con.execute(
            """SELECT 1 FROM paper_ledger WHERE side='SELL' AND ABS(quantity-?) < 0.0000000001
               AND ABS(price-?) < 0.0001""",
            (position['quantity'],position['exit_price']),
        ).fetchone()
        # A missing entry coupled to a missing matching exit can be repaired
        # exactly only when its recorded position fees cover both fills.
        exit_fee=(position['fees'] or 0)-(entry['fee'] or 0)
        if not has_entry and not has_exit and exit_fee >= -1e-8 and position['exit_reason']:
            missing.append({'position':position,'entry':entry,'exit_fee':max(0.0,exit_fee)})
    return missing


def missing_partial_realizations(con) -> list[dict]:
    """Find historical manual partial sells missing a closed-position record."""
    missing=[]
    for position in con.execute("SELECT * FROM positions WHERE status='closed'").fetchall():
        sells=con.execute(
            """SELECT * FROM orders WHERE side='SELL' AND reason='manual paper sell'
               AND created_at>? AND created_at<? ORDER BY created_at""",
            (position['opened_at'],position['closed_at']),
        ).fetchall()
        for sell in sells:
            synthetic='reconciliation-partial:'+sell['id']
            if con.execute('SELECT 1 FROM positions WHERE id=?',(synthetic,)).fetchone():
                continue
            total_quantity=position['quantity']+sell['quantity']
            entry=con.execute(
                """SELECT * FROM orders WHERE side='BUY' AND symbol=?
                   AND ABS(fill_price-?)<0.0001 AND created_at<=? ORDER BY created_at DESC LIMIT 1""",
                (position['symbol'],position['entry_price'],position['opened_at']),
            ).fetchone()
            if not entry or abs(entry['quantity']-total_quantity)>1e-8:
                continue
            allocated_entry_fee=entry['fee']*(sell['quantity']/total_quantity)
            realized=sell['quantity']*(sell['fill_price']-position['entry_price'])-allocated_entry_fee-sell['fee']
            missing.append({'id':synthetic,'position':position,'sell':sell,'entry_fee':allocated_entry_fee,'realized':realized})
    return missing


def repair_missing_ledger_events(path: Path) -> list[dict]:
    """Insert only source-backed historical ledger events; safe to rerun."""
    initialize(path, settings.starting_cash)
    repaired=[]
    with connect(path) as con:
        for item in missing_ledger_events(con):
            position,entry=item['position'],item['entry']
            marker=f"reconciliation:{position['id']}"
            if con.execute('SELECT 1 FROM paper_ledger WHERE note LIKE ?', (marker+'%',)).fetchone():
                continue
            con.execute(
                'INSERT INTO paper_ledger(created_at,side,quantity,price,fee,note,order_id) VALUES(?,?,?,?,?,?,?)',
                (position['opened_at'],'BUY',position['quantity'],position['entry_price'],entry['fee'],marker+':entry',entry['id']),
            )
            con.execute(
                'INSERT INTO paper_ledger(created_at,side,quantity,price,fee,note,order_id) VALUES(?,?,?,?,?,?,?)',
                (position['closed_at'],'SELL',position['quantity'],position['exit_price'],item['exit_fee'],marker+':'+position['exit_reason'],None),
            )
            repaired.append({'position_id':position['id'],'entry_fee':entry['fee'],'exit_fee':item['exit_fee']})
        for item in missing_partial_realizations(con):
            position,sell=item['position'],item['sell']
            con.execute(
                """INSERT INTO positions(id,strategy_id,symbol,opened_at,closed_at,quantity,entry_price,stop_price,take_price,
                   exit_price,realized_pnl,fees,exit_reason,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item['id'],position['strategy_id'],position['symbol'],position['opened_at'],sell['created_at'],sell['quantity'],
                 position['entry_price'],position['stop_price'],position['take_price'],sell['fill_price'],item['realized'],
                 item['entry_fee']+sell['fee'],'manual_partial_sell_reconciled','closed'),
            )
            repaired.append({'position_id':item['id'],'realized_pnl':item['realized'],'kind':'partial_realization'})
    return repaired


def report(path: Path) -> dict:
    initialize(path, settings.starting_cash)
    with connect(path) as con:
        account=con.execute("SELECT * FROM accounts WHERE id='btc-paper'").fetchone()
        snapshot=con.execute('SELECT * FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
        positions=con.execute('SELECT * FROM positions').fetchall()
        closed=[p for p in positions if p['status']=='closed']; opened=[p for p in positions if p['status']=='open']
        mark=snapshot['mark_price'] if snapshot else None
        realized=sum(p['realized_pnl'] or 0 for p in closed)
        # Mark-to-market has no hypothetical future closing fee: it therefore
        # reconciles directly to the current account equity.
        unrealized=sum(p['quantity']*((mark or p['entry_price'])-p['entry_price'])-(p['fees'] or 0) for p in opened)
        position_fees=sum(p['fees'] or 0 for p in positions)
        ledger_fees=con.execute('SELECT COALESCE(SUM(fee),0) FROM paper_ledger').fetchone()[0]
        equity=account['cash']+account['btc']*(mark or 0)
        return {'starting_equity':account['starting_cash'],'cash':account['cash'],'btc':account['btc'],'mark_price':mark,
                'equity':equity,'snapshot_equity':snapshot['equity'] if snapshot else None,'realized_pnl':realized,
                'unrealized_pnl':unrealized,'equity_change':equity-account['starting_cash'],
                'reconciliation_delta':equity-account['starting_cash']-realized-unrealized,
                'position_accounted_fees':position_fees,'ledger_recorded_fees':ledger_fees,
                'fee_delta':position_fees-ledger_fees,'missing_repairable_events':len(missing_ledger_events(con)),
                'missing_partial_realizations':len(missing_partial_realizations(con))}


def main():
    parser=argparse.ArgumentParser(description='Paper-trading reconciliation; no exchange actions.')
    parser.add_argument('--repair',action='store_true',help='insert only idempotent, source-backed missing ledger events')
    args=parser.parse_args()
    if args.repair:
        print(json.dumps({'repaired':repair_missing_ledger_events(settings.db_path)},indent=2))
    print(json.dumps(report(settings.db_path),indent=2))


if __name__=='__main__': main()
