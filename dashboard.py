import streamlit as st
import pandas as pd
from trading_agent.config import settings
from trading_agent.db import initialize, connect, utcnow

st.set_page_config(page_title='Paper Trading Research',layout='wide')
initialize(settings.db_path,settings.starting_cash)
st.title('Paper Trading Research Agent')
st.warning('SIMULATION ONLY — this application never submits real-money orders. Results are research hypotheses, not profitability claims.')
with connect(settings.db_path) as con:
    health=con.execute('SELECT * FROM worker_health WHERE id=1').fetchone()
    account=con.execute("SELECT * FROM accounts WHERE id='btc-paper'").fetchone()
    snap=con.execute('SELECT * FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
    strategies=con.execute('SELECT * FROM strategies').fetchall()
    positions=con.execute("SELECT * FROM positions WHERE status='open'").fetchall()
    decisions=con.execute('SELECT * FROM decisions ORDER BY id DESC LIMIT 30').fetchall()
    candles=con.execute("SELECT close_time,close FROM candles WHERE interval='1m' ORDER BY close_time DESC LIMIT 200").fetchall()
    errors=con.execute('SELECT * FROM quality_events ORDER BY id DESC LIMIT 10').fetchall()
cols=st.columns(4)
cols[0].metric('Worker',health['status'].upper())
cols[1].metric('Virtual cash',f"${account['cash']:,.2f}")
cols[2].metric('BTC held',f"{account['btc']:.8f}")
cols[3].metric('Equity',f"${snap['equity']:,.2f}" if snap else 'Awaiting market data')
st.caption(f"Last successful update: {health['last_success'] or 'none'} | EUR/USD: NOT CONFIGURED")
if health['last_error']: st.error(health['last_error'])
with connect(settings.db_path) as con:
    emergency=bool(con.execute("SELECT emergency_stop FROM accounts WHERE id='btc-paper'").fetchone()[0])
if st.toggle('Emergency stop: block new entries',value=emergency):
    with connect(settings.db_path) as con: con.execute("UPDATE accounts SET emergency_stop=1,updated_at=? WHERE id='btc-paper'",(utcnow(),))
else:
    with connect(settings.db_path) as con: con.execute("UPDATE accounts SET emergency_stop=0,updated_at=? WHERE id='btc-paper'",(utcnow(),))
st.subheader('Strategies')
st.dataframe(pd.DataFrame([dict(x) for x in strategies]),use_container_width=True)
st.subheader('BTC/USDT market')
if candles:
    frame=pd.DataFrame([dict(x) for x in candles]).iloc[::-1].set_index('close_time'); st.line_chart(frame['close'])
else: st.info('Awaiting validated public Binance candles.')
left,right=st.columns(2)
with left:
    st.subheader('Open positions'); st.dataframe(pd.DataFrame([dict(x) for x in positions]),use_container_width=True)
with right:
    st.subheader('Recent decisions'); st.dataframe(pd.DataFrame([dict(x) for x in decisions]),use_container_width=True)
if errors:
    st.subheader('Data / worker warnings'); st.dataframe(pd.DataFrame([dict(x) for x in errors]),use_container_width=True)
