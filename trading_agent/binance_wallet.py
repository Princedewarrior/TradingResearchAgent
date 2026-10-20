"""Strictly read-only Binance Spot account-data adapter.

This module intentionally has no order, cancel, transfer, withdrawal, or any
asset-changing method.  It signs only GET /api/v3/account server-side.
"""
from __future__ import annotations
import hashlib, hmac, json, os, time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

class WalletError(Exception): pass

class BinanceReadOnlyWallet:
    base='https://api.binance.com/api/v3'
    def __init__(self, key: str|None=None, secret: str|None=None):
        self.key=key if key is not None else os.getenv('BINANCE_API_KEY','')
        self.secret=secret if secret is not None else os.getenv('BINANCE_API_SECRET','')
    def configured(self): return bool(self.key and self.secret)
    def balances(self) -> list[dict]:
        if not self.configured(): raise WalletError('Read-only Binance wallet is not configured.')
        query=urlencode({'timestamp':int(time.time()*1000),'recvWindow':5000})
        signature=hmac.new(self.secret.encode(),query.encode(),hashlib.sha256).hexdigest()
        request=Request(self.base+'/account?'+query+'&signature='+signature,headers={'X-MBX-APIKEY':self.key},method='GET')
        try:
            with urlopen(request,timeout=12) as response: data=json.loads(response.read().decode())
        except HTTPError as e:
            if e.code in (401,403): raise WalletError('Binance rejected the read-only wallet credentials.') from e
            raise WalletError('Binance wallet service is temporarily unavailable.') from e
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            raise WalletError('Could not reach Binance wallet service.') from e
        try:
            return [{'asset':x['asset'],'free':float(x['free']),'locked':float(x['locked'])} for x in data['balances'] if float(x['free']) or float(x['locked'])]
        except (KeyError, ValueError, TypeError) as e: raise WalletError('Binance returned an invalid wallet response.') from e
