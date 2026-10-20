# TradingResearchAgent

Local, paper-only BTC/USDT research application. It gathers public Binance candles, validates and stores them in SQLite, records transparent rule-based decisions, and manages simulated positions. Binance remains read-only: no exchange-order, withdrawal, or transfer path exists.

## Highlights

- Live local dashboard with market status, paper-account activity, decisions, and trade reconstruction.
- Persistent SQLite storage for candles, decisions, orders, positions, snapshots, quality events, and worker health.
- Cost-aware BTC scalp research with fee/slippage assumptions, safety exits, sizing limits, and a persistent emergency stop.
- Isolated shadow monitoring for hypothetical long and short signals. Shadow signals never call paper-order execution functions.
- Cost-aware chronological backtesting with next-candle entries and conservative ambiguous-bar handling.
- Disabled-by-default MT5 demo preflight adapter. It inspects a locally logged-in demo terminal only and contains no order-submission code. See [MT5 demo setup](docs/MT5_DEMO_SETUP.md).

## Quick start (Windows PowerShell)

Install Python 3.12+ first, then run:

```powershell
cd C:\Users\dadir\Documents\TradingResearchAgent
.\scripts\start.ps1
```

The dashboard opens at `http://127.0.0.1:8501`.

To run one collection/decision cycle:

```powershell
.\.venv\Scripts\python.exe -m trading_agent.worker --once
```

Use `./scripts/status.ps1` to inspect worker health and `./scripts/stop.ps1` to stop the local processes.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use synthetic candles and do not call the internet.

## Security and research limitations

This is a research and simulation tool, not a profitability engine or financial advice. Backtests, short in-sample histories, and paper results do not establish an edge.

The repository excludes `.env`, SQLite databases, process locks/PIDs, virtual environments, and local agent metadata. Use `.env.example` as the non-secret configuration template. Never commit exchange, broker, or MT5 credentials.
