# MT5 / Exness demo preparation

This project is not connected to MT5 yet. The included adapter is inspection-only:
it cannot send, amend, or close orders.

## Before we connect together

1. Create or select an **Exness MT5 demo** account only. Do not use a real account.
2. Install the Windows MT5 desktop terminal from Exness and sign in to that demo account.
3. In MT5 Market Watch, find the broker's exact Bitcoin CFD symbol. It may not be `BTCUSD`.
4. Confirm its contract size, minimum volume, volume step, spread, commission, swap, and trading hours.
5. Copy `.env.example` to your private `.env`; set `MT5_ENABLED=1`, the terminal path, and the exact symbol. Never add credentials to source control.
6. Install the optional terminal bridge: `pip install MetaTrader5` in this project virtual environment.
7. Run `python -m trading_agent.mt5_demo`. It must report **Demo MT5 account verified** before any dashboard work begins.

## Safety gates for the later demo-execution phase

- Default: no execution after a restart.
- Refuse anything except an MT5 demo account.
- Independent emergency stop; open positions remain managed only if explicitly approved.
- One position maximum, minimum permitted demo lot, daily-loss cap, spread cap, cooldown, and consecutive-loss pause.
- Every broker request and deal ticket must be recorded. No credential, password, or account token is stored in SQLite.

The existing Binance path remains read-only and separate from MT5.
