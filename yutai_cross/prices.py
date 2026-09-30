"""株価の取得（任意機能・既定ではオフ）。

米Yahoo Finance のチャートAPI（公式に提供されたAPIではない非公開のエンドポイント）から
直近の株価を取る。利用規約上、自動取得が認められていない可能性があるので、
使う場合は個人の確認用途に限り、取得したデータを再配布しないこと。
おすすめは、証券会社からダウンロードした「コード,株価」の CSV を --prices で渡す方法。
仕様変更やアクセス制限で取れないことがあり、その場合はウォッチリストの「株価」列を使う。
貸株料の計算に使うだけなので、数%ずれていても判定への影響は小さい。
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Iterable, List, Optional, Tuple

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
USER_AGENT = "yutai-cross-planner/0.1 (personal use)"

Fetcher = Callable[[str], bytes]


def _http_get(url: str, timeout: float = 10.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def parse_yahoo_chart(payload: bytes) -> Optional[float]:
    data = json.loads(payload.decode("utf-8"))
    results = (data.get("chart") or {}).get("result") or []
    if not results:
        return None
    meta = results[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price is None:
        closes = (((results[0].get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
        closes = [c for c in closes if c is not None]
        price = closes[-1] if closes else None
    return float(price) if price is not None else None


def fetch_prices(
    codes: Iterable[str],
    fetch: Fetcher = _http_get,
    wait_seconds: float = 0.5,
) -> Tuple[Dict[str, float], List[str]]:
    """銘柄コードの株価を取得する。戻り値は (株価, エラーメッセージ一覧)。"""
    prices: Dict[str, float] = {}
    errors: List[str] = []
    for i, code in enumerate(dict.fromkeys(codes)):
        if i and wait_seconds:
            time.sleep(wait_seconds)
        url = YAHOO_CHART_URL.format(symbol=f"{code}.T")
        try:
            price = parse_yahoo_chart(fetch(url))
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError,
                KeyError, IndexError, TypeError, AttributeError) as e:
            errors.append(f"{code}: 株価を取得できませんでした（{e}）")
            continue
        if price is None:
            errors.append(f"{code}: 株価データが空でした")
        else:
            prices[code] = price
    return prices, errors
