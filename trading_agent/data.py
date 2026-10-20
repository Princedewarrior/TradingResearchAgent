from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

class DataError(Exception): pass

@dataclass(frozen=True)
class Candle:
    symbol: str; interval: str; open_time: str; close_time: str; open: float; high: float; low: float; close: float; volume: float

def validate_candles(candles: list[Candle], now: datetime | None=None) -> list[Candle]:
    if not candles: raise DataError('empty candle response')
    previous=None; valid=[]; now=now or datetime.now(timezone.utc)
    for c in candles:
        ot=datetime.fromisoformat(c.open_time); ct=datetime.fromisoformat(c.close_time)
        if ot.tzinfo != timezone.utc or ct.tzinfo != timezone.utc: raise DataError('timestamps must be UTC')
        if previous and ot <= previous: raise DataError('duplicate or out-of-order candle')
        if not (c.open>0 and c.high>0 and c.low>0 and c.close>0 and c.volume>=0): raise DataError('nonpositive price')
        if c.low > min(c.open,c.close) or c.high < max(c.open,c.close) or c.low>c.high: raise DataError('impossible OHLC')
        if ct > now: continue # incomplete current candle
        valid.append(c); previous=ot
    if not valid: raise DataError('no completed valid candles')
    return valid

class BinancePublicProvider:
    # Public read-only endpoints. These are data sources only; no trading API
    # client or authenticated endpoint exists in this project.
    bases=(
        'https://api.binance.com/api/v3',
        'https://api1.binance.com/api/v3',
        'https://api2.binance.com/api/v3',
        'https://data-api.binance.vision/api/v3',
    )
    def candles(self, symbol: str, interval: str, limit: int=200) -> list[Candle]:
        failure=None; rows=None
        for base in self.bases:
            try:
                url=base+'/klines?'+urlencode({'symbol':symbol,'interval':interval,'limit':limit})
                # Keep fallback failure bounded so the dashboard stays usable
                # during DNS outages or provider network failures.
                with urlopen(url,timeout=4) as response:
                    if response.status != 200: raise DataError(f'HTTP {response.status}')
                    rows=json.loads(response.read().decode('utf-8'))
                break
            except (URLError, HTTPError, TimeoutError, json.JSONDecodeError, DataError) as e:
                failure=e
        if rows is None:
            raise DataError(f'Binance request failed on all public endpoints: {failure}') from failure
        if not isinstance(rows,list): raise DataError('unexpected Binance response')
        out=[]
        for x in rows:
            out.append(Candle(symbol,interval,datetime.fromtimestamp(x[0]/1000,timezone.utc).isoformat(),datetime.fromtimestamp(x[6]/1000,timezone.utc).isoformat(),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])))
        return validate_candles(out)

class OandaPracticeProvider:
    """Intentional no-op unless explicitly configured; never fabricates EUR/USD prices."""
    def configured(self) -> bool: return False
