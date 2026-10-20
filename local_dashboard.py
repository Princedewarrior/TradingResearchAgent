"""Dependency-free local health dashboard. Paper trading only."""
from __future__ import annotations
import html, json
from datetime import datetime
from datetime import timezone
import uuid
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from trading_agent.config import settings
from trading_agent.db import initialize, connect, utcnow
from trading_agent.engine import PaperEngine
from trading_agent.binance_wallet import BinanceReadOnlyWallet, WalletError
from trading_agent.data import BinancePublicProvider, DataError
from trading_agent.strategy import btc_signal
from trading_agent.shadow import summary as shadow_summary
from trading_agent.backtest import run_btc_scalp_backtest

def latest_market_price():
    row=rows("SELECT close,close_time FROM candles WHERE symbol=? AND interval='1m' ORDER BY close_time DESC LIMIT 1",(settings.symbol,))
    if not row: return None, 'No validated BTC price is available yet.'
    stamp=datetime.fromisoformat(row[0]['close_time'])
    if (datetime.now(timezone.utc)-stamp).total_seconds()>settings.stale_seconds: return None, 'BTC price is stale; new paper entries are blocked.'
    return float(row[0]['close']), None

def sync_read_only_wallet():
    wallet=BinanceReadOnlyWallet(); now=utcnow()
    if not wallet.configured():
        with connect(settings.db_path) as con: con.execute("UPDATE live_wallet_sync SET status='not configured',last_error=NULL,updated_at=? WHERE id=1",(now,))
        return 'Not configured - add read-only credentials to .env.', []
    try:
        balances=wallet.balances()
        with connect(settings.db_path) as con:
            con.execute('DELETE FROM live_wallet_balances')
            con.executemany('INSERT INTO live_wallet_balances(asset,free,locked,synced_at) VALUES(?,?,?,?)',[(x['asset'],x['free'],x['locked'],now) for x in balances])
            con.execute("UPDATE live_wallet_sync SET status='connected',last_success=?,last_error=NULL,updated_at=? WHERE id=1",(now,now))
        return 'Connected - read only.',balances
    except WalletError as e:
        with connect(settings.db_path) as con: con.execute("UPDATE live_wallet_sync SET status='error',last_error=?,updated_at=? WHERE id=1",(str(e),now))
        return str(e),[]

def rows(sql, params=()):
    with connect(settings.db_path) as con:
        return [dict(x) for x in con.execute(sql,params).fetchall()]

def table(items, columns=None, labels=None):
    if not items: return '<p>None.</p>'
    columns=columns or list(items[0])
    labels=labels or {}
    head=''.join(f'<th>{html.escape(labels.get(x,str(x)))}</th>' for x in columns)
    body=''.join('<tr>'+''.join(f'<td>{html.escape(str(i.get(c,"")))}</td>' for c in columns)+'</tr>' for i in items)
    return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'

def friendly_time(value):
    if not value: return 'Not yet'
    try: return datetime.fromisoformat(value).strftime('%d %b, %H:%M UTC')
    except (TypeError, ValueError): return str(value)

def strategy_name(value):
    return {'btc_scalp':'BTC 15-minute scalp strategy','btc_swing':'BTC longer-trade strategy','eurusd_scalp':'EUR/USD quick-trade strategy','eurusd_swing':'EUR/USD longer-trade strategy'}.get(value,value)

def decision_label(value):
    return {'BUY':'Open long (paper)','SHORT':'Open short (paper)','SELL':'Close long (paper)','COVER':'Close short (paper)'}.get(value,'Hold - no trade')

def today_market_briefing(account, health, latest) -> str:
    """A factual session briefing, computed from persisted validated candles."""
    with connect(settings.db_path) as con:
        latest_candle=con.execute("SELECT * FROM candles WHERE symbol=? AND interval='1m' ORDER BY open_time DESC LIMIT 1",(settings.symbol,)).fetchone()
        if not latest_candle:
            return 'I cannot give a market briefing yet because no validated BTC candles are stored.'
        day=latest_candle['open_time'][:10]
        session=con.execute("SELECT open,high,low,close,volume FROM candles WHERE symbol=? AND interval='1m' AND open_time LIKE ? ORDER BY open_time",(settings.symbol,day+'%')).fetchall()
        open_count=con.execute("SELECT count(*) FROM positions WHERE status='open'").fetchone()[0]
        realized=con.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM positions WHERE status='closed'").fetchone()[0]
    first,last=session[0],session[-1]; high=max(x['high'] for x in session); low=min(x['low'] for x in session)
    change=(last['close']/first['open']-1)*100 if first['open'] else 0
    range_pct=(high/low-1)*100 if low else 0
    evaluation={}
    if latest and latest['evaluation_json']:
        try: evaluation=json.loads(latest['evaluation_json'])
        except (TypeError,ValueError): pass
    long=evaluation.get('long',{}); bearish=evaluation.get('bearish',{})
    long_state='ready' if long.get('passed') else 'not ready: '+', '.join(long.get('failed',[]))
    bearish_state='confirmed' if bearish.get('passed') else 'not confirmed: '+(', '.join(bearish.get('failed',[])) or 'no structured evaluation yet')
    candle_time=latest_candle['close_time']
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(candle_time)).total_seconds()
    freshness='fresh' if age <= settings.stale_seconds else f'stale by {max(0, int(age-settings.stale_seconds))} seconds'
    return (f"Today's BTC/USDT session ({day} UTC): price is ${last['close']:,.2f}, "
            f"{change:+.2f}% from the first stored 1-minute candle. The session range is ${low:,.2f} to ${high:,.2f} ({range_pct:.2f}%). "
            f"The latest validated candle closed at {friendly_time(candle_time)} and is {freshness}; the worker is {health['status']} and last updated at {friendly_time(health['last_success'])}. "
            f"The current 15-minute scalp long setup is {long_state}; bearish conditions are {bearish_state}. "
            f"Paper account: ${account['cash']:,.2f} cash, {account['btc']:.6f} BTC, {open_count} open position(s), and ${realized:,.2f} realized P&L. "
            "This describes the measured session; it is not a promise that the next move will be profitable.")

def profitability_briefing() -> str:
    """Report a reproducible backtest result from persisted, validated data."""
    with connect(settings.db_path) as con:
        stored=con.execute("SELECT * FROM candles WHERE symbol=? AND interval='1m' ORDER BY open_time", (settings.symbol,)).fetchall()
    from trading_agent.data import Candle
    candles=[Candle(x['symbol'],x['interval'],x['open_time'],x['close_time'],x['open'],x['high'],x['low'],x['close'],x['volume']) for x in stored]
    if len(candles) < 31:
        return f'I cannot assess profitability yet: only {len(candles)} validated one-minute candles are stored, but this rule needs at least 31 to test one next-candle entry.'
    result=run_btc_scalp_backtest(candles, settings)
    sample_ok=result.candle_count >= 43_200 and result.trade_count >= 100
    verdict='does not establish a profitable edge' if not sample_ok else ('was net profitable in this in-sample window only' if result.net_pnl > 0 else 'was not profitable in this in-sample window')
    win_rate='n/a' if result.win_rate_pct is None else f'{result.win_rate_pct:.1f}%'
    avg_win='n/a' if result.average_win is None else f'${result.average_win:.2f}'
    avg_loss='n/a' if result.average_loss is None else f'${result.average_loss:.2f}'
    return (f'The rule-based BTC scalp backtest used {result.candle_count} persisted one-minute candles and produced {result.trade_count} completed trade(s). '
            f'Net P&L was ${result.net_pnl:.2f} ({result.net_return_pct:+.2f}%), win rate {win_rate}, average win {avg_win}, average loss {avg_loss}, '
            f'max drawdown {result.max_drawdown_pct:.2f}%, modeled fees ${result.fees_paid:.2f}, and modeled slippage cost ${result.slippage_cost:.2f}. '
            f'This {verdict}. It enters only on the next candle after a completed-candle signal, charges configured fees/slippage on both sides, and resolves same-bar stop/target conflicts as a stop. '
            'It still needs a longer, independent out-of-sample dataset and realistic latency/liquidity assumptions before it can support a profitability claim.')

def agent_reply(message: str) -> str:
    """Local, read-only research conversation. It cannot submit any order."""
    text=message.lower().strip()
    with connect(settings.db_path) as con:
        account=con.execute("SELECT * FROM accounts WHERE id='btc-paper'").fetchone()
        health=con.execute('SELECT * FROM worker_health WHERE id=1').fetchone()
        opens=con.execute("SELECT count(*) FROM positions WHERE status='open'").fetchone()[0]
        latest=con.execute('SELECT action,reason,created_at,market_time,evaluation_json,duration_ms FROM decisions ORDER BY id DESC LIMIT 1').fetchone()
        latest_exit=con.execute("SELECT closed_at,exit_reason,exit_price FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT 1").fetchone()
    is_fall_question=any(x in text for x in ('fall','fell','bearish','down','short')) and any(x in text for x in ('why','didn\'t','did not','without'))
    if any(x in text for x in ('profit','profitable','make money','make a profit','does it work','backtest','edge')):
        return profitability_briefing()
    if any(x in text for x in ('how is today','today market','today?','today','market today','session')):
        return today_market_briefing(account,health,latest)
    if is_fall_question:
        if not latest or not latest['evaluation_json']:
            return 'I do not have a structured evaluation for that historical moment, so I will not guess why no trade was made. The next completed-candle evaluation will record its timestamp, indicator values, long and bearish checks, and exit result.'
        try: evaluation=json.loads(latest['evaluation_json'])
        except (TypeError, ValueError):
            return 'The latest decision record does not contain readable measured evaluation data, so I cannot give a factual explanation for that fall.'
        indicators=evaluation.get('indicators',{}); bearish=evaluation.get('bearish',{}); exits=evaluation.get('exit_checks',[])
        measures=', '.join(f'{name.replace("_"," ")}={value:.4f}' if isinstance(value,float) else f'{name}={value}' for name,value in indicators.items()) or 'no indicator values recorded'
        failed=', '.join(bearish.get('failed',[])) or 'all bearish checks passed'
        if exits:
            exit_result='; '.join(x['result'] for x in exits)
        elif latest_exit:
            exit_result=f"the most recent prior exit was {latest_exit['exit_reason']} at ${latest_exit['exit_price']:,.2f} on {friendly_time(latest_exit['closed_at'])}"
        else: exit_result='there was no open long position to evaluate for exit'
        return f"The latest valid evaluation was at {friendly_time(latest['market_time'] or latest['created_at'])}. Price was ${latest['price'] if 'price' in latest.keys() else indicators.get('price',0):,.2f}; regime: {evaluation.get('regime','uncertain')}. Bearish conditions were evaluated, but the bearish setup {'passed' if bearish.get('passed') else 'did not pass'}: {failed}. Measured values: {measures}. The current spot strategy cannot profit from a new short because the separate paper-short module is disabled. I evaluated the existing long position for an exit, and the result was: {exit_result}."
    if any(x in text for x in ('good to','should i','buy','sell','trade now','enter now','place a trade')):
        with connect(settings.db_path) as con:
            rows=con.execute("SELECT * FROM candles WHERE symbol=? AND interval='1m' ORDER BY open_time DESC LIMIT 60",(settings.symbol,)).fetchall()
        if len(rows)<30:
            return 'I cannot assess the current setup because there is not enough validated 1-minute history yet. I would wait rather than force a paper trade.'
        from trading_agent.data import Candle
        candles=[Candle(x['symbol'],x['interval'],x['open_time'],x['close_time'],x['open'],x['high'],x['low'],x['close'],x['volume']) for x in reversed(rows)]
        latest_time=datetime.fromisoformat(candles[-1].close_time)
        if (datetime.now(timezone.utc)-latest_time).total_seconds()>settings.stale_seconds:
            return f'The latest validated BTC candle is from {friendly_time(candles[-1].close_time)}, which is stale. The safe answer is to wait; new paper entries are blocked until fresh data returns.'
        action,reason,stop,take=btc_signal(candles)
        if action=='BUY':
            return f'The current scalp rule set sees a qualified paper-entry setup at about ${candles[-1].close:,.2f}: {reason}. Its tight safety exit is ${stop:,.2f}, target is ${take:,.2f}, and it exits after {settings.scalp_max_hold_minutes} minutes if neither is reached. This is a test hypothesis, not a guarantee or personal recommendation.'
        return f'The current scalp rule set says wait at about ${candles[-1].close:,.2f}. Reason: {reason}. There is no qualified short-horizon entry under the configured EMA/momentum rules, so forcing a trade would go against the system rules.'
    if any(x in text for x in ('balance','wallet','cash','money')):
        return f'Your separate paper wallet has ${account["cash"]:,.2f} virtual cash and {account["btc"]:.6f} simulated BTC. Live Binance balances are read-only and never fund paper trades.'
    if any(x in text for x in ('position','open trade','holding')):
        return f'There {"is" if opens==1 else "are"} {opens} open simulated position{"" if opens==1 else "s"}. Any open position is managed by its stored stop, target, and protection rules.'
    if any(x in text for x in ('either up or down','up or down','both direction','both directions','long and short')):
        if not latest or not latest['evaluation_json']:
            return 'The strategy now evaluates both long and bearish confirmation conditions, but I need a fresh structured evaluation before I can describe the current setup.'
        evaluation=json.loads(latest['evaluation_json']); long=evaluation.get('long',{}); bearish=evaluation.get('bearish',{})
        return f"Yes—both directions are evaluated. The latest regime was {evaluation.get('regime','uncertain')}. Long setup: {'confirmed' if long.get('passed') else 'not confirmed ('+', '.join(long.get('failed',[]))+')'}. Bearish setup: {'confirmed' if bearish.get('passed') else 'not confirmed ('+', '.join(bearish.get('failed',[]))+')'}. A confirmed bearish setup is recorded, but it cannot open a position until the separately tested paper-short module is deliberately enabled."
    if any(x in text for x in ('status','market','connection','live')):
        return f'The worker is {health["status"]}. Last successful market update: {friendly_time(health["last_success"])}. New paper entries are blocked whenever validated market data is stale or unavailable.'
    if any(x in text for x in ('decision','why','signal','wait')) and latest:
        action='waited' if latest['action']=='HOLD' else latest['action'].lower()+'ed'
        age=' Its market snapshot is from '+friendly_time(latest['market_time'])+'.' if latest['market_time'] else ''
        return f'The latest agent decision {action} at {friendly_time(latest["created_at"])}. Recorded reason: {latest["reason"]}.{age} A wait decision is normal when conditions do not meet the configured research rules.'
    if any(x in text for x in ('hint','help','what can you','what should i ask','guide')):
        decision = f'The latest decision was {latest["action"]}: {latest["reason"]}.' if latest else 'There is no recorded decision yet.'
        safety = 'New paper entries are currently stopped.' if account['emergency_stop'] else 'New paper entries are enabled, subject to the strategy risk checks.'
        return f'Quick hint: {decision} You have {opens} open paper position{"s" if opens!=1 else ""}. {safety} Try “market status”, “why did it wait?”, “open positions”, or the explicit simulated command “paper buy 25”.'
    return (f'I did not understand “{message[:120]}” well enough to invent an answer. Right now the worker is {health["status"]} and there are {opens} open paper positions. '
            'Try asking about today’s market, the latest decision, a specific price move, your open trade, or the paper balance.')

def execute_agent_command(message: str) -> str | None:
    """Explicit commands limited to the existing paper execution service."""
    command=message.lower().strip()
    with connect(settings.db_path) as con:
        if command in ('emergency stop','stop paper trading','enable emergency stop'):
            con.execute("UPDATE accounts SET emergency_stop=1,updated_at=? WHERE id='btc-paper'",(utcnow(),))
            return 'Emergency stop is now ON. New paper entries are blocked; open paper positions can still exit safely.'
        if command in ('resume paper trading','disable emergency stop','turn off emergency stop'):
            con.execute("UPDATE accounts SET emergency_stop=0,updated_at=? WHERE id='btc-paper'",(utcnow(),))
            return 'Emergency stop is now OFF. New entries still require fresh market data and all normal risk checks.'
        match=re.fullmatch(r'paper\s+(buy|sell)\s+\$?([0-9]+(?:\.[0-9]{1,2})?)',command)
        if not match: return None
        side,raw=match.groups(); notional=float(raw)
        price,error=latest_market_price()
        if notional<=0 or not price or error: return 'Command not executed: '+(error or 'amount must be positive')
        engine=PaperEngine(settings); key='agent-command:'+str(uuid.uuid4())
        if side=='buy': result=engine.enter(con,'agent_command',key,price,price*.98,price*1.03,'agent chat paper buy')
        else: result=engine.manual_sell(con,key,notional/price,price,'agent chat paper sell')
        engine.record_snapshot(con,price)
        return f'Paper {side} command for ${notional:,.2f}: {result}. This was simulated only; nothing was sent to Binance.'

def page():
    initialize(settings.db_path,settings.starting_cash)
    health=rows('SELECT * FROM worker_health WHERE id=1')[0]
    account=rows("SELECT * FROM accounts WHERE id='btc-paper'")[0]
    latest=rows('SELECT * FROM snapshots ORDER BY id DESC LIMIT 1')
    pos=rows("SELECT * FROM positions WHERE status='open'")
    decisions=rows('SELECT created_at,strategy_id,action,reason,price,status FROM decisions ORDER BY id DESC LIMIT 25')
    warnings=rows('SELECT created_at,source,message FROM quality_events ORDER BY id DESC LIMIT 10')
    strategies=rows('SELECT id,market,timeframe,enabled FROM strategies ORDER BY id')
    equity=latest[0]['equity'] if latest else 'Awaiting market data'
    checked='checked' if account['emergency_stop'] else ''
    pnl=(equity-account['starting_cash']) if not isinstance(equity,str) else 0
    worker_label='Live and checking the market' if health['status']=='running' else 'Not receiving market updates'
    strategy_cards=''.join(f'''<div class="strategy"><div><b>{'BTC scalp' if s['id']=='btc_scalp' else 'BTC swing' if s['id']=='btc_swing' else 'EUR/USD scalp' if s['id']=='eurusd_scalp' else 'EUR/USD swing'}</b><span>{s['market']} · {s['timeframe']}</span></div><em class="{'on' if s['enabled'] else 'off'}">{'ACTIVE' if s['enabled'] else 'OFF'}</em></div>''' for s in strategies)
    human_decisions=[]
    for d in decisions:
        human_decisions.append({'When':friendly_time(d['created_at']),'Strategy':strategy_name(d['strategy_id']),'Decision':'Buy BTC (paper)' if d['action']=='BUY' else 'Wait — no trade','Why':'The market trend and recent price movement supported a cautious paper entry.' if d['action']=='BUY' else 'The setup is not strong enough yet, so the agent is waiting.','BTC price':f"${d['price']:,.2f}" if d['price'] else 'Not available','Result':'Paper order filled' if d['status']=='filled' else 'Recorded — no action'})
    human_positions=[]
    for p in pos:
        value=p['quantity']*p['entry_price']
        human_positions.append({'Strategy':strategy_name(p['strategy_id']),'Asset':'Bitcoin','Amount':f"{p['quantity']:.6f} BTC (${value:,.2f})",'Bought at':f"${p['entry_price']:,.2f}",'Safety exit':f"${p['stop_price']:,.2f}",'Profit target':f"${p['take_price']:,.2f}",'Opened':friendly_time(p['opened_at'])})
    human_warnings=[{'When':friendly_time(w['created_at']),'Area':'Market connection' if w['source']=='worker' else w['source'].replace('_',' ').title(),'Message':w['message']} for w in warnings]
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>Trading Research</title><style>
*{{box-sizing:border-box}} body{{font-family:Inter,Segoe UI,system-ui,sans-serif;margin:0;background:#f5f7fb;color:#172033}} header{{background:#101828;color:white;padding:22px max(24px,calc((100% - 1180px)/2))}} header h1{{font-size:22px;margin:0 0 5px}} header p{{margin:0;color:#cdd5df;font-size:14px}} main{{max-width:1180px;margin:0 auto;padding:26px 24px 48px}} .notice{{display:flex;justify-content:space-between;gap:16px;align-items:center;background:#fff7e8;border:1px solid #fed7aa;color:#7c2d12;padding:14px 16px;border-radius:10px;margin-bottom:22px;font-size:14px}} .status{{display:inline-flex;align-items:center;gap:7px;font-weight:600;color:#067647}} .dot{{width:9px;height:9px;border-radius:50%;background:#12b76a}} .grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}} .metric,.panel{{background:white;border:1px solid #e4e7ec;border-radius:12px;padding:18px;box-shadow:0 1px 2px #1018280a}} .metric label{{display:block;font-size:13px;color:#667085;margin-bottom:8px}} .metric strong{{font-size:24px;font-weight:650}} .metric small{{display:block;margin-top:5px;color:#667085}} .green{{color:#067647}} .red{{color:#b42318}} .layout{{display:grid;grid-template-columns:1.15fr .85fr;gap:16px;margin-top:16px}} h2{{font-size:16px;margin:0 0 6px}} .subtitle{{color:#667085;font-size:13px;margin:0 0 16px}} .strategy{{display:flex;align-items:center;justify-content:space-between;padding:13px 0;border-top:1px solid #eaecf0}} .strategy:first-of-type{{border-top:0}} .strategy span{{display:block;color:#667085;font-size:13px;margin-top:3px}} em{{font-style:normal;font-size:11px;font-weight:700;letter-spacing:.03em;padding:5px 8px;border-radius:20px}} .on{{background:#ecfdf3;color:#067647}} .off{{background:#f2f4f7;color:#667085}} .action{{background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;padding:14px;margin-top:14px}} button{{border:0;border-radius:7px;background:#b42318;color:#fff;padding:9px 12px;font-weight:600;cursor:pointer;margin-left:8px}} .helper{{font-size:13px;color:#667085;line-height:1.55}} table{{width:100%;border-collapse:collapse;font-size:13px}} th{{text-align:left;color:#667085;font-weight:600;padding:10px 8px;border-bottom:1px solid #eaecf0}} td{{padding:10px 8px;border-bottom:1px solid #f2f4f7;vertical-align:top}} .wide{{margin-top:16px}} .empty{{color:#667085;padding:10px 0}} @media(max-width:760px){{.grid{{grid-template-columns:repeat(2,1fr)}}.layout{{grid-template-columns:1fr}}.notice{{align-items:flex-start;flex-direction:column}}}} @media(max-width:420px){{.grid{{grid-template-columns:1fr}}main{{padding:18px 14px}}}}
</style></head><body><header><h1>Trading Research</h1><p>BTC/USDT paper simulation - automatic decisions are research hypotheses, not financial advice.</p></header><main><div class="notice"><span><b>Paper mode only.</b> No real-money orders can be placed from this app.</span><span class="status"><i class="dot"></i>{worker_label}</span></div><section class="grid"><div class="metric"><label>Your paper account</label><strong>${equity if isinstance(equity,str) else format(equity,',.2f')}</strong><small>Starting balance: ${account['starting_cash']:,.2f}</small></div><div class="metric"><label>Profit / loss</label><strong class="{'green' if pnl>=0 else 'red'}">{'+' if pnl>=0 else ''}${pnl:,.2f}</strong><small>Simulated only</small></div><div class="metric"><label>Cash available</label><strong>${account['cash']:,.2f}</strong><small>Ready for a new paper trade</small></div><div class="metric"><label>Bitcoin currently held</label><strong>{account['btc']:.6f}</strong><small>{len(pos)} open position{'s' if len(pos)!=1 else ''}</small></div></section><section class="layout"><div class="panel"><h2>What the agent is doing</h2><p class="subtitle">It checks Bitcoin once a minute. It trades only when the setup is strong enough and the risk limits allow it.</p><div class="strategy"><div><b>Latest market update</b><span>{friendly_time(health['last_success'])}</span></div><em class="on">{health['status'].upper()}</em></div><div class="strategy"><div><b>Current position</b><span>{'A paper Bitcoin position is being managed with a safety exit and a profit target.' if pos else 'No open trade - the agent is waiting for a safer opportunity.'}</span></div></div><div class="strategy"><div><b>EUR/USD</b><span>No provider is connected, so this market is not used.</span></div><em class="off">NOT SET UP</em></div><div class="action"><b>Emergency stop</b><div class="helper">Turn this on to stop new paper trades. Any open trade can still close safely at its protective level.</div><form method="post" action="/emergency"><label class="helper"><input type="checkbox" name="enabled" {checked}> Stop new paper trades</label><button>{'Save emergency stop' if account['emergency_stop'] else 'Save setting'}</button></form></div></div><div class="panel"><h2>Strategies</h2><p class="subtitle">Each strategy is independent. Disabled strategies cannot make trades.</p>{strategy_cards}</div></section><section class="panel wide"><h2>Latest decisions</h2><p class="subtitle">Wait - no trade is normal. The agent should not trade just to be active.</p>{table(human_decisions)}</section><section class="panel wide"><h2>Open paper trades</h2>{table(human_positions)}</section>{('<section class="panel wide"><h2>Things needing attention</h2>'+table(human_warnings)+'</section>') if warnings else ''}</main></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        parsed=urlparse(self.path)
        if parsed.path == '/api/candles':
            interval=parse_qs(parsed.query).get('interval',['1m'])[0]
            if interval not in ('1m','5m','15m','1h','4h'):
                self.send_error(400,'Unsupported timeframe'); return
            try:
                candles=BinancePublicProvider().candles(settings.symbol,interval,180)
                payload={'ok':True,'source':'Binance Spot public API','interval':interval,'candles':[{'time':c.open_time,'open':c.open,'high':c.high,'low':c.low,'close':c.close,'volume':c.volume} for c in candles]}
            except DataError as e:
                payload={'ok':False,'error':'Market data unavailable. New paper entries remain blocked until a fresh validated price is available.'}
            body=json.dumps(payload).encode('utf-8')
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(body); return
        if self.path == '/health':
            payload=json.dumps(rows('SELECT * FROM worker_health WHERE id=1')[0]).encode()
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(payload); return
        if parsed.path == '/api/state':
            # Small local-only feed for in-place dashboard updates. It reads
            # the database only; it never triggers a Binance wallet action.
            account=rows("SELECT cash,btc,emergency_stop FROM accounts WHERE id='btc-paper'")[0]
            health=rows('SELECT status,last_success,last_error FROM worker_health WHERE id=1')[0]
            latest=rows('SELECT action,reason,price,created_at,market_time FROM decisions ORDER BY id DESC LIMIT 1')
            open_longs=rows("SELECT count(*) AS count FROM positions WHERE status='open'")[0]['count']
            open_shorts=rows("SELECT count(*) AS count FROM paper_short_positions WHERE status='open'")[0]['count']
            price,price_error=latest_market_price()
            payload={'ok':True,'server_time':utcnow(),'price':price,'price_error':price_error,'account':account,'health':health,
                     'open_positions':{'long':open_longs,'short':open_shorts},'latest_decision':latest[0] if latest else None}
            body=json.dumps(payload).encode('utf-8')
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(body); return
        if parsed.path == '/api/shadow':
            with connect(settings.db_path) as con: payload={'ok':True,**shadow_summary(con)}
            body=json.dumps(payload).encode('utf-8')
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(body); return
        # Some Windows installations have a broken local "Inter" font whose
        # character map renders ordinary letters as symbols. Override it here.
        initialize(settings.db_path,settings.starting_cash)
        wallet_message,wallet_balances=sync_read_only_wallet()
        price,price_error=latest_market_price()
        health=rows('SELECT status,last_success,last_error FROM worker_health WHERE id=1')[0]
        with connect(settings.db_path) as con:
            account=dict(con.execute("SELECT * FROM accounts WHERE id='btc-paper'").fetchone())
            ledger=[dict(x) for x in con.execute('SELECT created_at,side,quantity,price,fee,note FROM paper_ledger ORDER BY id DESC LIMIT 12').fetchall()]
        wallet_rows=''.join(f'<tr><td>{html.escape(x["asset"])}</td><td>{x["free"]:,.8f}</td><td>{x["locked"]:,.8f}</td></tr>' for x in wallet_balances) or '<tr><td colspan="3">No live balances shown.</td></tr>'
        activity=''.join(f'<div class="attention-item"><b>{x["side"]} - paper only</b><span>{friendly_time(x["created_at"])}</span><p>{x["quantity"]:.6f} BTC at ${x["price"]:,.2f}; estimated fee ${x["fee"]:,.2f}. {html.escape(x["note"])}</p></div>' for x in ledger) or '<p class="muted">No paper trading activity yet.</p>'
        chart_panel='''<section class="panel chart-panel"><div class="chart-head"><div><h2>BTC/USDT live market</h2><p id="chartStatus" class="subtitle">Loading validated Binance Spot candles...</p></div><div class="timeframes"><button type="button" data-tf="1m">1m</button><button type="button" data-tf="5m">5m</button><button type="button" data-tf="15m">15m</button><button type="button" data-tf="1h">1h</button><button type="button" data-tf="4h">4h</button></div></div><canvas id="candleChart" height="430" aria-label="BTC USDT candlestick chart with volume"></canvas><p class="helper">Live stream updates the current candle in place. History, zoom, and your chart position are preserved.</p></section>'''
        latest_reason=rows('SELECT reason FROM decisions ORDER BY id DESC LIMIT 1')
        entry_state='PAUSED — protective exits still active' if account['emergency_stop'] else 'ENABLED — paper entries allowed'
        terminal=chart_panel+f'''<section class="panel live-state" aria-live="polite"><b>Live terminal state</b><span id="livePrice">BTC: {'$'+format(price,',.2f') if price else 'waiting for price'}</span><span id="liveWorker">Worker: {health['status']}</span><span id="liveEntryState">New entries: {entry_state}</span><span id="livePortfolio">Paper cash: ${account['cash']:,.2f} | BTC: {account['btc']:.6f}</span><span id="liveDecision">Latest: {html.escape(str(latest_reason[0]['reason'])) if latest_reason else 'awaiting evaluation'}</span><small id="liveUpdated">Updates in place every 10 seconds</small></section><section class="terminal"><div class="panel"><h2>BTC/USDT paper terminal</h2><p class="subtitle">Market price: <b>{'$'+format(price,',.2f') if price else 'Unavailable'}</b> - {html.escape(price_error or 'Fresh validated market data')}</p><form method="post" action="/paper-order" class="order-form"><label>Amount in USDT <input name="notional" type="number" min="5" step="0.01" value="25" required></label><button name="side" value="BUY" {'disabled' if not price else ''}>Paper buy</button><button name="side" value="SELL" {'disabled' if not price else ''}>Paper sell</button></form><p class="helper">Buy spends simulated USDT. Sell sells simulated Bitcoin only; it cannot create a short position.</p></div><div class="panel"><h2>Live Binance balance - read only</h2><p class="subtitle">{html.escape(wallet_message)}</p><table><thead><tr><th>Asset</th><th>Available</th><th>Locked</th></tr></thead><tbody>{wallet_rows}</tbody></table><p class="helper">This wallet is never used to fund paper trades and has no trading, transfer, or withdrawal controls.</p></div></section><section class="panel wide"><h2>Paper activity</h2><p class="subtitle">Orders below are simulated. Nothing is sent to Binance.</p>{activity}</section>'''
        content=page().replace('<section class="layout">',terminal+'<section class="layout">')
        attention=rows('SELECT id,created_at,source,message FROM quality_events WHERE id IN (SELECT MAX(id) FROM quality_events GROUP BY source,message) ORDER BY id DESC LIMIT 20')
        open_trades=rows("SELECT id,strategy_id,symbol,quantity,entry_price,stop_price,take_price,opened_at FROM positions WHERE status='open' ORDER BY opened_at DESC")
        closed_trades=rows("SELECT * FROM positions WHERE status='closed' ORDER BY closed_at DESC")
        with connect(settings.db_path) as con: shadow=shadow_summary(con)
        recent_decisions=rows('SELECT created_at,strategy_id,action,reason,price,status FROM decisions ORDER BY id DESC LIMIT 25')
        count=len(attention)
        items=''.join(f'<div class="attention-item"><b>{html.escape(str(x["source"]).replace("_"," ").title())}</b><span>{html.escape(friendly_time(x["created_at"]))}</span><p>{html.escape(str(x["message"]))}</p></div>' for x in attention) or '<p class="muted">Everything looks clear right now.</p>'
        latest=attention[0] if attention else None
        toast=(f'<div id="toast" class="toast" data-notice-id="{latest["id"]}"><b>Attention needed</b><span>{html.escape(str(latest["message"]))}</span></div>' if latest else '')
        nav=f'''<header><div class="topbar"><div><h1>TradingResearchAgent</h1><p>Live BTC/USDT research terminal · paper execution only</p></div><details class="menu"><summary>Dashboard menu <span>⌄</span></summary><div class="menu-items"><button class="menu-button" type="button" onclick="openModal('tradesModal');this.closest('details').removeAttribute('open')">Open trades <span>{len(open_trades)}</span></button><button class="menu-button" type="button" onclick="openModal('closedTradesModal');this.closest('details').removeAttribute('open')">Closed trade reconstruction <span>{len(closed_trades)}</span></button><button class="menu-button" type="button" onclick="openModal('shadowModal');this.closest('details').removeAttribute('open')">Shadow signals <span>{shadow['open']+shadow['pending']}</span></button><button class="menu-button" type="button" onclick="openModal('decisionsModal');this.closest('details').removeAttribute('open')">Latest decisions</button><button class="attention-button" type="button" onclick="openModal('attentionModal');this.closest('details').removeAttribute('open')" aria-label="Open things needing attention">Attention <span>{count}</span></button></div></details></div></header>'''
        trade_items=''.join(f'''<div class="attention-item"><b>Bitcoin paper trade</b><span>Opened {html.escape(friendly_time(x['opened_at']))}</span><p><b>Amount:</b> {x['quantity']:.6f} BTC (${x['quantity']*x['entry_price']:,.2f})</p><p>Bought at ${x['entry_price']:,.2f} | Safety exit ${x['stop_price']:,.2f} | Profit target ${x['take_price']:,.2f}</p><form method="post" action="/close-position"><input type="hidden" name="position_id" value="{html.escape(x['id'])}"><button class="close-trade">Close this paper trade</button></form></div>''' for x in open_trades) or '<p class="muted">There are no open paper trades. The agent is waiting for a safe setup.</p>'
        decision_items=''.join(f'''<div class="attention-item"><b>{decision_label(x['action'])}</b><span>{html.escape(friendly_time(x['created_at']))} | {html.escape(strategy_name(x['strategy_id']))}</span><p>{html.escape(x['reason'])}</p><p>Bitcoin price: ${x['price']:,.2f} | {'Paper order filled' if x['status']=='filled' else 'No action taken'}</p></div>''' for x in recent_decisions) or '<p class="muted">No decisions have been recorded yet.</p>'
        def reconstruction_item(x):
            minutes=(datetime.fromisoformat(x['closed_at'])-datetime.fromisoformat(x['opened_at'])).total_seconds()/60
            gross=(x['exit_price']-x['entry_price'])*x['quantity']; net=x['realized_pnl']; tone='trade-profit' if net>=0 else 'trade-loss'
            exit_label={'stop_loss':'Protective stop / trailing stop','take_profit':'Profit target','scalp_time_exit':'Scalp time exit','manual_sell':'Manual sell','manual individual paper close':'Manual close'}.get(x['exit_reason'],x['exit_reason'] or 'Not recorded')
            return f'''<div class="attention-item reconstruction"><b>{html.escape(strategy_name(x['strategy_id']))} <strong class="{tone}">{'+' if net>=0 else ''}${net:,.2f}</strong></b><span>Opened {html.escape(friendly_time(x['opened_at']))} · Closed {html.escape(friendly_time(x['closed_at']))} · {minutes:.1f} min</span><p><b>{x['quantity']:.6f} BTC</b> | Entry ${x['entry_price']:,.2f} → Exit ${x['exit_price']:,.2f}</p><p>Gross ${gross:,.2f} | Fees ${x['fees']:,.2f} | Net <b>{'+' if net>=0 else ''}${net:,.2f}</b></p><p>Exit: {html.escape(exit_label)}</p></div>'''
        reconstruction_items=''.join(reconstruction_item(x) for x in closed_trades) or '<p class="muted">No closed paper trades yet.</p>'
        shadow_items=''.join(f'''<div class="attention-item"><b>{x['direction']} — HYPOTHETICAL <strong class="{'trade-profit' if (x['net_pnl'] or 0)>=0 else 'trade-loss'}">{('+' if (x['net_pnl'] or 0)>=0 else '')+('$'+format(x['net_pnl'],'.2f')) if x['net_pnl'] is not None else x['status']}</strong></b><span>Signal {html.escape(friendly_time(x['signal_time']))} | {html.escape(x['strategy_id'])} | {x['status']}</span><p>Signal ${x['signal_price']:,.2f} | {'Entry $'+format(x['entry_price'],',.2f') if x['entry_price'] else 'Waiting for next-candle fill'} | {'Exit $'+format(x['exit_price'],',.2f') if x['exit_price'] else 'Observation open'}</p><p>Fees ${x['entry_fee']+x['exit_fee']:,.2f} | MFE ${x['mfe']:,.2f} | MAE ${x['mae']:,.2f} | {html.escape(x['exit_reason'] or 'No exit yet')}</p></div>''' for x in shadow['observations']) or '<p class="muted">Monitoring is active. No qualifying hypothetical signal yet.</p>'
        shadow_latest=shadow['latest_evaluation']
        shadow_fresh=(f"BTC ${shadow_latest['price']:,.2f} at {html.escape(friendly_time(shadow_latest['market_time']))}" if shadow_latest else 'Waiting for the first completed candle')
        shadow_header=f"<div id='shadowSummary' class='shadow-summary'>SIMULATED / HYPOTHETICAL ONLY - {shadow['closed']} finalized - net ${shadow['net_pnl']:,.2f} - fees ${shadow['fees']:,.2f}<br><small id='shadowFreshness'>Data: {shadow_fresh} | worker: {html.escape(shadow['worker_health']['status']) if shadow['worker_health'] else 'unknown'} | connection failures: {shadow['connection_failures']}</small></div>"
        def modal_box(identifier, title, subtitle, inner):
            return f'''<div id="{identifier}" class="modal-backdrop" role="dialog" aria-modal="true"><div class="modal"><div class="modal-title"><div><h2>{title}</h2><p>{subtitle}</p></div><button type="button" class="close" onclick="closeModal('{identifier}')" aria-label="Close">Close</button></div><div class="attention-list">{inner}</div></div></div>'''
        messages=rows('SELECT role,message,created_at FROM agent_messages ORDER BY id DESC LIMIT 30')[::-1]
        chat_items=''.join(f'<div class="chat {html.escape(m["role"])}"><span>{"Agent" if m["role"]=="agent" else "You"}</span><p>{html.escape(m["message"])}</p></div>' for m in messages) or '<div class="chat agent"><span>Agent</span><p>Hello. Ask me about the market, your paper wallet, open positions, decisions, or safety controls.</p></div>'
        agent_modal=modal_box('agentModal','Chat with Agent','Ask questions or use explicit paper commands: “paper buy 25”, “paper sell 25”, “emergency stop”, or “resume paper trading”. Never real trading.',chat_items+'''<form id="agentChatForm" class="chat-form"><div class="chat-input-wrap"><input id="agentChatInput" name="message" maxlength="600" placeholder="Ask about your paper trading research..." required><button aria-label="Send message" title="Send message"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 3 3 10.3l7 2.7 2.7 7L21 3Z"></path><path d="m10 13 4-4"></path></svg></button></div></form>''')
        launcher='''<button class="chat-launcher" type="button" onclick="openModal('agentModal')" aria-label="Chat with Agent"><span class="chat-icon" aria-hidden="true"></span><span>Chat with Agent</span></button>'''
        emergency_action = '''<form method="post" action="/emergency"><input type="hidden" name="enabled" value="1"><button>Stop new paper trades</button></form>''' if not account['emergency_stop'] else '''<form method="post" action="/emergency"><button class="resume-trading">Resume new paper trades</button></form>'''
        emergency_state = 'New paper trades are currently enabled.' if not account['emergency_stop'] else 'New paper trades are currently stopped.'
        emergency_controls=f'''<div class="trade-safety"><b>Trading safety</b><p>{emergency_state} Stopping new entries does not close your existing paper trades. Use the close button on an individual trade if you want to exit it.</p>{emergency_action}</div>'''
        modal=agent_modal+modal_box('attentionModal','Things needing attention','Recent market-data and worker messages.',items)+modal_box('tradesModal','Open paper trades','These are simulated Bitcoin trades. The amount in brackets is the dollar value at entry.',trade_items+emergency_controls)+modal_box('closedTradesModal','Closed trade reconstruction','Completed simulated trades with entry/exit prices, holding time, gross P/L, fees, net P/L, and exit type.',reconstruction_items)+modal_box('shadowModal','Shadow signals','SIMULATED / HYPOTHETICAL ONLY. These observations cannot place, modify, or close paper trades.',shadow_header+shadow_items)+modal_box('decisionsModal','Latest decisions','The agent often chooses to wait. That is a normal risk-control decision.',decision_items)+launcher+toast+'''<script>function openModal(id){document.getElementById(id).classList.add('visible')}function closeModal(id){document.getElementById(id).classList.remove('visible')}document.querySelectorAll('.modal-backdrop').forEach(function(m){m.addEventListener('click',function(e){if(e.target===m)m.classList.remove('visible')})});const form=document.getElementById('agentChatForm');if(form){form.addEventListener('submit',async function(e){e.preventDefault();const input=document.getElementById('agentChatInput'),value=input.value.trim();if(!value)return;const list=form.closest('.attention-list');function add(role,text){form.insertAdjacentHTML('beforebegin','<div class="chat '+role+'"><span>'+ (role==='user'?'You':'Agent') +'</span><p></p></div>');form.previousElementSibling.querySelector('p').textContent=text;list.scrollTop=list.scrollHeight}add('user',value);input.value='';try{const r=await fetch('/agent-chat',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},body:'message='+encodeURIComponent(value)}),j=await r.json();add('agent',j.reply||'I could not respond just now.')}catch(_){add('agent','Message could not be sent. Please try again.')}})}const toast=document.getElementById('toast');if(toast){const id=toast.dataset.noticeId;const key='lastAttentionNotice';if(localStorage.getItem(key)!==id){toast.classList.add('show');localStorage.setItem(key,id);setTimeout(()=>toast.classList.remove('show'),6000)}}</script>'''
        content=content.replace('<header><h1>Trading Research</h1><p>BTC/USDT paper simulation - automatic decisions are research hypotheses, not financial advice.</p></header>', nav)
        content=content.replace('<section class="panel wide"><h2>Things needing attention</h2>', '<section class="panel wide attention-legacy"><h2>Things needing attention</h2>')
        content=content.replace('<section class="panel wide"><h2>Latest decisions</h2>', '<section class="panel wide attention-legacy"><h2>Latest decisions</h2>')
        content=content.replace('<section class="panel wide"><h2>Open paper trades</h2>', '<section class="panel wide attention-legacy"><h2>Open paper trades</h2>')
        content=content.replace('</body>', modal+'</body>')
        content=content.replace('</head>', '''<style>body{font-family:Arial,Helvetica,sans-serif !important}.topbar{max-width:1180px;margin:auto;display:flex;justify-content:space-between;gap:20px;align-items:center}.attention-button{background:#fff;color:#172033;margin:0;border:0;padding:10px 14px}.attention-button span{background:#b42318;color:#fff;border-radius:12px;padding:2px 7px;margin-left:6px}.attention-legacy{display:none}.modal-backdrop{display:none;position:fixed;inset:0;background:#10182899;z-index:10;padding:24px;align-items:center;justify-content:center}.modal-backdrop.visible{display:flex}.modal{background:#fff;color:#172033;width:min(640px,100%);max-height:75vh;overflow:auto;border-radius:12px;padding:22px}.modal-title{display:flex;justify-content:space-between;gap:16px;align-items:start;border-bottom:1px solid #eaecf0;padding-bottom:14px}.modal-title h2{margin:0}.modal-title p,.muted{color:#667085;font-size:13px}.close{background:#f2f4f7;color:#172033;margin:0}.attention-item{padding:14px 0;border-bottom:1px solid #eaecf0}.attention-item span{display:block;color:#667085;font-size:12px;margin-top:3px}.attention-item p{margin:7px 0 0;font-size:14px}.toast{display:none;position:fixed;right:22px;bottom:22px;z-index:20;background:#172033;color:#fff;border-radius:10px;padding:14px 16px;width:min(390px,calc(100% - 44px));box-shadow:0 10px 28px #10182855}.toast.show{display:block;animation:toastin .25s ease-out}.toast b{display:block;margin-bottom:5px}.toast span{font-size:13px;color:#eaecf0}@keyframes toastin{from{transform:translateY(20px);opacity:0}to{transform:translateY(0);opacity:1}}@media(max-width:600px){.topbar{align-items:start;flex-direction:column}.attention-button{width:100%}}</style></head>''')
        content=content.replace('</head>', '''<style>
body,main,.panel,.metric,.modal{color:#172033!important} .panel,.metric{background:#fff!important}
.metric label,.metric small,.subtitle,.helper,.strategy span,.modal-title p,.attention-item span{color:#475467!important}
@media(max-width:760px){header{padding:18px 16px!important}main{padding:16px 12px 32px!important}.topbar{align-items:stretch!important}.topbar h1{font-size:20px!important}.topbar p{font-size:13px!important}.notice{font-size:13px!important}.grid{grid-template-columns:1fr 1fr!important;gap:10px!important}.metric,.panel{padding:14px!important}.metric strong{font-size:20px!important}.layout{grid-template-columns:1fr!important;gap:12px!important}.strategy{align-items:flex-start!important;gap:10px!important}.strategy em{flex-shrink:0}.wide{overflow-x:auto!important}.wide table{min-width:620px}.modal-backdrop{padding:12px!important}.modal{padding:16px!important;max-height:85vh!important}.toast{right:12px!important;bottom:12px!important;width:calc(100% - 24px)!important}.attention-button{width:100%!important}}
@media(max-width:390px){.grid{grid-template-columns:1fr!important}.metric strong{font-size:22px!important}}
</style></head>''')
        content=content.replace('</head>', '''<style>
.chat-launcher{background:#111a20!important;border:1px solid #3c6872!important;color:#eef7fa!important;box-shadow:0 10px 28px #00000055!important;border-radius:18px!important;min-width:112px}.chat-icon{width:24px;height:18px;border:2px solid #9ed5dc;border-radius:6px;display:block;position:relative}.chat-icon:after{content:"";position:absolute;bottom:-6px;left:5px;width:7px;height:7px;border-left:2px solid #9ed5dc;border-bottom:2px solid #9ed5dc;transform:skewY(-35deg);background:#111a20}.chat-launcher>span:last-child{font-size:13px;color:#eef7fa}.chat-form{position:sticky;bottom:0;background:#0e1a20;padding:14px 0 2px;margin:0;z-index:2}.chat-input-wrap{position:relative;width:100%}.chat-form input{width:100%;padding:12px 52px 12px 12px!important}.chat-form button{position:absolute;right:5px;top:5px;width:34px;height:34px;padding:0;border-radius:7px;background:#1a3941!important;display:grid;place-items:center}.chat-form button svg{width:18px;height:18px;fill:none;stroke:#d9f5f5;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}#agentModal .attention-list{max-height:55vh;overflow-y:auto;padding-right:4px}#agentModal .modal{max-height:78vh}.chat{box-shadow:none!important}.chat.agent{background:#13262c!important;border-color:#254c55!important}.chat.user{background:#1a2b3b!important;border-color:#35536b!important}
.action{display:none!important}.trade-safety{margin-top:16px;padding-top:14px;border-top:1px solid #29404a}.trade-safety p{font-size:13px;color:#9ab5c0;line-height:1.5}.trade-safety form,.attention-item form{display:inline-block;margin:6px 8px 0 0}.trade-safety button{margin:0;background:#b54708!important}.trade-safety .resume-trading{background:#1a3941!important;color:#e8f0f5!important}.close-trade{margin:0;background:#9d2e2e!important;color:#fff!important}
</style></head>''')
        content=content.replace('</head>', '''<style>.menu{position:relative;min-width:190px}.menu summary{list-style:none;cursor:pointer;background:#152a33;color:#fff;border:1px solid #2a5862;border-radius:8px;padding:10px 14px;font-weight:600}.menu summary::-webkit-details-marker{display:none}.menu summary span{float:right;margin-left:20px}.menu[open] summary span{display:inline-block;transform:rotate(180deg)}.menu-items{position:absolute;right:0;top:calc(100% + 8px);z-index:8;display:grid;gap:7px;min-width:260px;padding:10px;background:#0e1a20;border:1px solid #2a5862;border-radius:10px;box-shadow:0 16px 35px #00000066}.menu-button{background:#152a33!important;color:#fff!important;margin:0!important;text-align:left}.menu-button span{background:#fff;color:#172033;border-radius:12px;padding:2px 7px;margin-left:6px}.attention-button{margin:0!important;text-align:left}@media(max-width:760px){.menu{width:100%}.menu-items{left:0;right:0;min-width:0}.menu-items button{width:100%}}</style></head>''')
        content=content.replace('</head>', '''<style>.chart-panel{margin-bottom:16px;background:#101518!important;color:#edf4f8!important}.chart-panel h2{color:#fff!important}.chart-head{display:flex;justify-content:space-between;gap:12px;align-items:start}.chart-head .subtitle{color:#aab7c4!important}.timeframes{display:flex;gap:6px;flex-wrap:wrap}.timeframes button{margin:0;background:#263238;color:#edf4f8;padding:7px 10px}.timeframes button:first-child{background:#167b5f}#candleChart{display:block;width:100%;height:430px;border-radius:8px;margin-top:8px}.terminal{display:grid;grid-template-columns:1.1fr .9fr;gap:16px;margin-bottom:16px}.order-form{display:flex;align-items:end;gap:10px;flex-wrap:wrap}.order-form label{font-size:13px;color:#475467;display:grid;gap:5px}.order-form input{padding:10px;border:1px solid #d0d5dd;border-radius:7px;width:150px}.order-form button{margin:0}.order-form button[value=BUY]{background:#067647}.order-form button:disabled{background:#98a2b3}@media(max-width:760px){.terminal{grid-template-columns:1fr}.chart-head{flex-direction:column}#candleChart{height:360px}}</style></head>''')
        content=content.replace('</head>', '''<script>window.__interactiveChartEnabled=true;</script><script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script><style>#interactiveChart{height:430px;width:100%;margin-top:8px}#candleChart{display:none!important}.chart-ohlc{color:#aab7c4;font-size:12px;margin:4px 0}.chart-live{color:#20c997!important}@media(max-width:760px){#interactiveChart{height:360px}}</style><script>
window.addEventListener('DOMContentLoaded',function(){
 const canvas=document.getElementById('candleChart'), holder=document.createElement('div'), status=document.getElementById('chartStatus'); holder.id='interactiveChart'; canvas.before(holder);
 if(!window.LightweightCharts){status.textContent='Interactive chart library could not load. Check network access to unpkg.com.';return}
 const ohlc=document.createElement('div');ohlc.className='chart-ohlc';holder.before(ohlc);
 let chart=LightweightCharts.createChart(holder,{layout:{background:{color:'#101518'},textColor:'#aab7c4'},grid:{vertLines:{color:'#1c272c'},horzLines:{color:'#1c272c'}},rightPriceScale:{borderColor:'#37474f'},timeScale:{borderColor:'#37474f',timeVisible:true,secondsVisible:false},crosshair:{mode:LightweightCharts.CrosshairMode.Normal},handleScroll:true,handleScale:true});
 let candles=chart.addCandlestickSeries({upColor:'#20c997',downColor:'#ef5350',borderVisible:false,wickUpColor:'#20c997',wickDownColor:'#ef5350'}); let volume=chart.addHistogramSeries({priceFormat:{type:'volume'},priceScaleId:'vol'}); volume.priceScale().applyOptions({scaleMargins:{top:.78,bottom:0}});
 let tf='1m',ws=null,retry=0,timer=null,lastUpdate=0;
 const normalise=a=>[...new Map(a.filter(x=>x&&x.time&&[x.open,x.high,x.low,x.close,x.volume].every(Number.isFinite)).sort((a,b)=>a.time-b.time).map(x=>[x.time,x])).values()];
 function setStatus(t,live){status.textContent=t;status.classList.toggle('chart-live',!!live)}
 function resize(){chart.applyOptions({width:holder.clientWidth,height:holder.clientHeight})} new ResizeObserver(resize).observe(holder);
 chart.subscribeCrosshairMove(p=>{if(!p.time||!p.seriesData.get(candles)){ohlc.textContent='Move over a candle to inspect open, high, low, close, and volume.';return}let d=p.seriesData.get(candles),v=p.seriesData.get(volume);ohlc.textContent=new Date(p.time*1000).toLocaleString()+' | O '+d.open+' H '+d.high+' L '+d.low+' C '+d.close+' | Volume '+(v?v.value:'-')});
 async function history(){setStatus('Loading validated Binance Spot history...',false);let r=await fetch('/api/candles?interval='+tf,{cache:'no-store'}),j=await r.json();if(!j.ok)throw Error('market unavailable');let d=normalise(j.candles.map(x=>({time:Math.floor(new Date(x.time).getTime()/1000),open:+x.open,high:+x.high,low:+x.low,close:+x.close,volume:+x.volume})));if(!d.length)throw Error('empty history');candles.setData(d.map(({volume,...x})=>x));volume.setData(d.map(x=>({time:x.time,value:x.volume,color:x.close>=x.open?'#20c99788':'#ef535088'})));chart.timeScale().fitContent();setStatus('Live Binance Spot | '+tf+' | '+d.length+' validated candles',true)}
 function update(k){let d={time:Math.floor(k.t/1000),open:+k.o,high:+k.h,low:+k.l,close:+k.c,volume:+k.v};if(!Number.isFinite(d.close))return;candles.update(d);volume.update({time:d.time,value:d.volume,color:d.close>=d.open?'#20c99788':'#ef535088'});lastUpdate=Date.now();setStatus('Live Binance Spot stream | '+tf+' | updating current candle',true)}
 function connect(){if(ws)ws.close();clearTimeout(timer);setStatus('Connecting to Binance live stream...',false);ws=new WebSocket('wss://stream.binance.com:9443/ws/btcusdt@kline_'+tf);ws.onopen=()=>{retry=0;setStatus('Live Binance Spot stream | '+tf,true)};ws.onmessage=e=>{try{let m=JSON.parse(e.data);if(m.k)update(m.k)}catch(_){}};ws.onerror=()=>ws.close();ws.onclose=()=>{setStatus('Live stream reconnecting; chart history remains available.',false);if(retry<5){timer=setTimeout(connect,Math.min(30000,1000*2**retry++))}else fallback()}}
 async function fallback(){setStatus('Live stream unavailable; checking REST safely without resetting chart.',false);try{let r=await fetch('/api/candles?interval='+tf),j=await r.json();if(j.ok){let x=j.candles.at(-1);update({t:new Date(x.time).getTime(),o:x.open,h:x.high,l:x.low,c:x.close,v:x.volume})}}catch(_){} timer=setTimeout(fallback,15000)}
 async function switchTf(next){tf=next;if(ws)ws.close();clearTimeout(timer);try{await history();connect()}catch(e){setStatus('Market data unavailable - no stale or fabricated chart is shown.',false)}}
 document.querySelectorAll('[data-tf]').forEach(b=>b.onclick=()=>switchTf(b.dataset.tf));switchTf(tf);setInterval(()=>{if(lastUpdate&&Date.now()-lastUpdate>45000)setStatus('Chart is stale - waiting for a market update.',false)},5000);
});</script></head>''')
        content=content.replace('</head>', '''<style>
body{background:radial-gradient(circle at 15% -10%,#173144 0,#071016 38%,#05090d 100%)!important;color:#e8f0f5!important}header{background:linear-gradient(100deg,#08151c,#102a32 55%,#071016)!important;border-bottom:1px solid #1f434c}header p{color:#9ab5c0!important}.panel,.metric{background:linear-gradient(145deg,#101c23,#0c151b)!important;border-color:#203842!important;box-shadow:0 16px 35px #00000030!important;color:#e8f0f5!important}.metric label,.metric small,.subtitle,.helper,.strategy span,.modal-title p,.attention-item span{color:#9ab5c0!important}.metric strong{color:#f2f8fa!important}.notice{background:#12242b!important;border-color:#265764!important;color:#d7edf1!important}.menu-button{background:#152a33!important;border:1px solid #2a5862!important}.attention-button{background:#d6b45f!important;color:#111b20!important}.agent-button{background:linear-gradient(120deg,#20c997,#3ea9e8)!important;color:#031217!important;font-weight:700!important;margin:0!important;box-shadow:0 0 20px #20c99755}.modal{background:#0e1a20!important;color:#e8f0f5!important;border:1px solid #28505b}.modal-title{border-color:#29404a}.close{background:#20343d!important;color:#e8f0f5!important}.attention-item{border-color:#233942}.terminal .panel h2{color:#eaf7fa!important}.order-form input{background:#071016!important;color:#e8f0f5!important;border-color:#315762!important}.chat{padding:12px 14px;margin:10px 0;border-radius:10px;max-width:92%;line-height:1.45}.chat span{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#88d7c4}.chat p{margin:5px 0 0}.chat.agent{background:#122a31;border:1px solid #215c62}.chat.user{background:#1c2d47;border:1px solid #2e5784;margin-left:auto}.chat.user span{color:#9cc8ff}.chat-form{display:flex;gap:8px;border-top:1px solid #29404a;padding-top:14px;margin-top:12px}.chat-form input{flex:1;background:#071016;color:#e8f0f5;border:1px solid #315762;border-radius:8px;padding:11px}.chat-form button{margin:0;background:#20a984;color:#041312}.green{color:#46e0a9!important}.red{color:#ff7b76!important}@media(max-width:760px){.chat-form{flex-direction:column}.chat-form button{width:100%}}
body{background:radial-gradient(circle at 15% -10%,#173144 0,#071016 38%,#05090d 100%)!important;color:#e8f0f5!important}header{background:linear-gradient(100deg,#08151c,#102a32 55%,#071016)!important;border-bottom:1px solid #1f434c}header p{color:#9ab5c0!important}.panel,.metric{background:linear-gradient(145deg,#101c23,#0c151b)!important;border-color:#203842!important;box-shadow:0 16px 35px #00000030!important;color:#e8f0f5!important}.metric label,.metric small,.subtitle,.helper,.strategy span,.modal-title p,.attention-item span{color:#9ab5c0!important}.metric strong{color:#f2f8fa!important}.notice{background:#12242b!important;border-color:#265764!important;color:#d7edf1!important}.menu-button{background:#152a33!important;border:1px solid #2a5862!important}.attention-button{background:#d6b45f!important;color:#111b20!important}.agent-button{background:linear-gradient(120deg,#20c997,#3ea9e8)!important;color:#031217!important;font-weight:700!important;margin:0!important;box-shadow:0 0 20px #20c99755}.modal{background:#0e1a20!important;color:#e8f0f5!important;border:1px solid #28505b}.modal-title{border-color:#29404a}.close{background:#20343d!important;color:#e8f0f5!important}.attention-item{border-color:#233942}.terminal .panel h2{color:#eaf7fa!important}.order-form input{background:#071016!important;color:#e8f0f5!important;border-color:#315762!important}.chat{padding:12px 14px;margin:10px 0;border-radius:10px;max-width:92%;line-height:1.45}.chat span{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#88d7c4}.chat p{margin:5px 0 0}.chat.agent{background:#122a31;border:1px solid #215c62}.chat.user{background:#1c2d47;border:1px solid #2e5784;margin-left:auto}.chat.user span{color:#9cc8ff}.chat-form{display:flex;gap:8px;border-top:1px solid #29404a;padding-top:14px;margin-top:12px}.chat-form input{flex:1;background:#071016;color:#e8f0f5;border:1px solid #315762;border-radius:8px;padding:11px}.chat-form button{margin:0;background:#20a984;color:#041312}.chat-launcher{position:fixed;right:22px;bottom:22px;z-index:9;display:flex;flex-direction:column;align-items:center;gap:5px;background:linear-gradient(135deg,#20c997,#3ea9e8)!important;color:#031217!important;border:0;border-radius:18px;padding:12px 16px!important;font-weight:700;box-shadow:0 14px 32px #00000066,0 0 22px #20c99755}.chat-icon{font-size:12px;font-weight:800;letter-spacing:.05em}.chat-launcher>span:last-child{font-size:13px}.green{color:#46e0a9!important}.red{color:#ff7b76!important}@media(max-width:760px){.chat-form{flex-direction:column}.chat-form button{width:100%}.chat-launcher{right:14px;bottom:14px;border-radius:15px;padding:10px 13px!important}}
</style></head>''')
        content=content.replace('</head>', '''<style>
#agentModal .attention-list{display:block!important;overflow-y:auto!important;padding-bottom:4px}
#agentModal .chat-form{display:block!important;position:sticky!important;bottom:0!important;margin:0!important;padding:12px 0 2px!important;background:#0e1a20!important;z-index:5!important}
#agentModal .chat-input-wrap{display:block!important;position:relative!important;width:100%!important;min-height:48px!important}
#agentModal .chat-form input{display:block!important;width:100%!important;height:46px!important;padding:11px 52px 11px 12px!important;background:#071016!important;color:#e8f0f5!important;opacity:1!important;visibility:visible!important}
#agentModal .chat-form button{display:grid!important;position:absolute!important;right:6px!important;top:6px!important;width:34px!important;height:34px!important;margin:0!important;padding:0!important}
</style></head>''')
        content=content.replace('</body>', '''<script>
async function refreshLiveState(){
  try{
    const response=await fetch('/api/state',{cache:'no-store'}); const state=await response.json(); if(!state.ok)return;
    const money=n=>Number(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
    const set=(id,value)=>{const node=document.getElementById(id);if(node)node.textContent=value};
    set('livePrice',state.price==null?'BTC: waiting for fresh price':'BTC: $'+money(state.price));
    set('liveWorker','Worker: '+state.health.status+(state.health.last_success?' | last update '+new Date(state.health.last_success).toLocaleTimeString():''));
    set('liveEntryState','New entries: '+(state.account.emergency_stop?'PAUSED — protective exits still active':'ENABLED — paper entries allowed'));
    set('livePortfolio','Paper cash: $'+money(state.account.cash)+' | BTC: '+Number(state.account.btc).toFixed(6)+' | Open longs: '+state.open_positions.long+' | Open shorts: '+state.open_positions.short);
    if(state.latest_decision)set('liveDecision','Latest: '+state.latest_decision.action+' — '+state.latest_decision.reason);
    set('liveUpdated','Updated '+new Date(state.server_time).toLocaleTimeString()+' • live feed checks every 10 seconds');
  }catch(_){set('liveUpdated','Live update paused; keeping the last known safe state.')}
}
refreshLiveState(); setInterval(refreshLiveState,10000);
async function refreshShadowState(){try{const r=await fetch('/api/shadow',{cache:'no-store'}),s=await r.json(),n=document.getElementById('shadowSummary');if(n&&s.ok){const e=s.latest_evaluation;const fresh=e?'BTC $'+Number(e.price).toLocaleString()+' at '+new Date(e.market_time).toLocaleTimeString():'Waiting for the first completed candle';n.innerHTML='SIMULATED / HYPOTHETICAL ONLY - '+s.closed+' finalized - net $'+Number(s.net_pnl).toFixed(2)+' - fees $'+Number(s.fees).toFixed(2)+' - open '+s.open+' - pending '+s.pending+'<br><small>Data: '+fresh+' | worker: '+(s.worker_health?s.worker_health.status:'unknown')+' | connection failures: '+s.connection_failures+'</small>'}}catch(_){}}
refreshShadowState();setInterval(refreshShadowState,10000);
</script></body>''')
        content=content.replace('</head>', '''<style>
.live-state{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;margin-bottom:16px}.live-state b{color:#88d7c4}.live-state span{font-size:13px}.live-state small{width:100%;color:#9ab5c0}
.reconstruction strong{float:right}.trade-profit{color:#46e0a9}.trade-loss{color:#ff8b86}
.shadow-summary{padding:10px;border:1px solid #265764;border-radius:8px;background:#10262c;color:#9ee7d1;font-size:13px}
@media(max-width:600px){.live-state{gap:8px}.live-state span{width:100%}}
</style></head>''')
        body=b'\xef\xbb\xbf'+content.encode('utf-8')
        self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        n=int(self.headers.get('Content-Length','0')); form=parse_qs(self.rfile.read(n).decode())
        if self.path == '/agent-chat':
            message=form.get('message',[''])[0].strip()[:600]
            reply='Please enter a message.'
            if message:
                reply=execute_agent_command(message) or agent_reply(message)
                with connect(settings.db_path) as con:
                    con.execute('INSERT INTO agent_messages(created_at,role,message) VALUES(?,?,?)',(utcnow(),'user',message))
                    con.execute('INSERT INTO agent_messages(created_at,role,message) VALUES(?,?,?)',(utcnow(),'agent',reply))
            if self.headers.get('Accept','').lower().find('application/json')>=0:
                body=json.dumps({'reply':reply}).encode('utf-8')
                self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8'); self.end_headers(); self.wfile.write(body); return
            self.send_response(303); self.send_header('Location','/'); self.end_headers(); return
        if self.path == '/close-position':
            position_id=form.get('position_id',[''])[0]
            price,error=latest_market_price()
            if position_id and price and not error:
                with connect(settings.db_path) as con:
                    result=PaperEngine(settings).close_position(con,position_id,price,'manual individual paper close')
                    PaperEngine(settings).record_snapshot(con,price)
                    con.execute('INSERT INTO quality_events(created_at,source,message) VALUES(?,?,?)',(utcnow(),'paper terminal','Individual paper-trade close: '+result))
            self.send_response(303); self.send_header('Location','/'); self.end_headers(); return
        if self.path == '/paper-order':
            try: notional=float(form.get('notional',['0'])[0])
            except ValueError: notional=0
            price,error=latest_market_price()
            if notional<=0 or not price or error:
                self.send_response(303); self.send_header('Location','/'); self.end_headers(); return
            side=form.get('side',[''])[0]
            with connect(settings.db_path) as con:
                engine=PaperEngine(settings); key='manual:'+str(uuid.uuid4())
                if side=='BUY':
                    status=engine.enter(con,'manual_paper',key,price,price*.98,price*1.03,'manual paper buy')
                elif side=='SELL':
                    status=engine.manual_sell(con,key,notional/price,price)
                else: status='rejected: invalid side'
                engine.record_snapshot(con,price)
                con.execute('INSERT INTO quality_events(created_at,source,message) VALUES(?,?,?)',(utcnow(),'paper terminal',f'Manual {side} ${notional:,.2f}: {status}'))
            self.send_response(303); self.send_header('Location','/'); self.end_headers(); return
        if self.path != '/emergency': self.send_error(404); return
        enabled='enabled' in form
        with connect(settings.db_path) as con: con.execute("UPDATE accounts SET emergency_stop=?,updated_at=? WHERE id='btc-paper'",(int(enabled),utcnow()))
        self.send_response(303); self.send_header('Location','/'); self.end_headers()

if __name__ == '__main__':
    initialize(settings.db_path,settings.starting_cash)
    print('Paper dashboard listening on http://127.0.0.1:8501',flush=True)
    ThreadingHTTPServer(('127.0.0.1',8501),Handler).serve_forever()
