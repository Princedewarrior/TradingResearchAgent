$root=Split-Path -Parent $PSScriptRoot
Set-Location $root
& .venv\Scripts\python.exe -c "from trading_agent.config import settings; from trading_agent.db import connect,initialize; initialize(settings.db_path,settings.starting_cash); exec('with connect(settings.db_path) as c: print(dict(c.execute(\"select * from worker_health where id=1\").fetchone()))')"
