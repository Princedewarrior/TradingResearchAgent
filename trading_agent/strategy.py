from __future__ import annotations
from .data import Candle

def ema(values: list[float], period: int) -> float:
    if len(values)<period: raise ValueError('not enough candles')
    value=sum(values[:period])/period; alpha=2/(period+1)
    for point in values[period:]: value=alpha*point+(1-alpha)*value
    return value

def atr(candles: list[Candle], period: int=14) -> float:
    if len(candles)<=period: raise ValueError('not enough candles')
    ranges=[max(c.high-c.low,abs(c.high-p.close),abs(c.low-p.close)) for p,c in zip(candles[-period-1:-1],candles[-period:])]
    return sum(ranges)/len(ranges)

def rsi(values: list[float], period: int=14) -> float:
    if len(values)<=period: raise ValueError('not enough candles')
    changes=[b-a for a,b in zip(values[-period-1:-1],values[-period:])]
    gain=sum(x for x in changes if x>0)/period; loss=-sum(x for x in changes if x<0)/period
    return 100.0 if loss==0 else 100-(100/(1+gain/loss))

def btc_evaluation(candles: list[Candle], scalp_stop_pct: float=.0025, scalp_take_pct: float=.0035) -> dict:
    """Short-horizon BTC scalp evaluation using only completed candles."""
    closes=[c.close for c in candles]
    if len(closes)<30:
        return {'action':'HOLD','reason':'insufficient history','market_time':candles[-1].close_time if candles else None,
                'regime':'uncertain','indicators':{},'long':{'passed':False,'failed':['fewer than 30 completed candles']},
                'bearish':{'passed':False,'failed':['fewer than 30 completed candles']},'stop':0,'take':0,'short_stop':0,'short_take':0}
    fast,slow=ema(closes,5),ema(closes,13); momentum=closes[-1]/closes[-3]-1; average_range=atr(candles); current_rsi=rsi(closes)
    long_checks={'ema_5_above_ema_13':fast>slow,'two_candle_momentum_above_0_03pct':momentum>0.0003,'rsi_not_overbought':current_rsi<72}
    bearish_checks={'ema_5_below_ema_13':fast<slow,'two_candle_momentum_below_minus_0_03pct':momentum<-0.0003,'rsi_not_oversold':current_rsi>28}
    long_failed=[name for name, passed in long_checks.items() if not passed]
    bearish_failed=[name for name, passed in bearish_checks.items() if not passed]
    regime='bullish' if fast>slow and momentum>0 else 'bearish' if fast<slow and momentum<0 else 'ranging'
    values={'price':closes[-1],'ema_10':fast,'ema_25':slow,'momentum_5':momentum,'rsi_14':current_rsi,'atr_14':average_range}
    if not long_failed:
        return {'action':'BUY','reason':'all long-entry checks passed','market_time':candles[-1].close_time,'regime':regime,'indicators':values,
                'long':{'passed':True,'checks':long_checks,'failed':[]},'bearish':{'passed':not bearish_failed,'checks':bearish_checks,'failed':bearish_failed},'stop':closes[-1]*(1-scalp_stop_pct),'take':closes[-1]*(1+scalp_take_pct),'short_stop':closes[-1]*(1+scalp_stop_pct),'short_take':closes[-1]*(1-scalp_take_pct),'style':'short-horizon scalp'}
    return {'action':'HOLD','reason':'long entry not confirmed: '+', '.join(long_failed),'market_time':candles[-1].close_time,'regime':regime,'indicators':values,
            'long':{'passed':False,'checks':long_checks,'failed':long_failed},'bearish':{'passed':not bearish_failed,'checks':bearish_checks,'failed':bearish_failed},'stop':0,'take':0,'short_stop':closes[-1]*(1+scalp_stop_pct),'short_take':closes[-1]*(1-scalp_take_pct)}

def btc_signal(candles: list[Candle]) -> tuple[str,str,float,float]:
    """Backward-compatible adapter for callers that only need an order signal."""
    result=btc_evaluation(candles)
    return result['action'],result['reason'],result['stop'],result['take']
