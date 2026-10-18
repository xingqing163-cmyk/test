"""Yahoo!ショッピング 商品検索API（v3）で、新品・在庫ありの商品を安い順に探す。

使うには Yahoo!デベロッパーネットワークで Client ID（アプリケーションID）を発行する。
JANコードがあればJANで、なければ商品名で検索する。
ポイントはAPIが返す付与ポイント（通常・ボーナスの大きい方）を使う。
APIに出てこない自分だけの上乗せ（カード払いやキャンペーン）は設定の extra_point_rate で足す。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

API_URL = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
USER_AGENT = "kaitori-check/0.1 (personal use)"

Fetcher = Callable[[str], bytes]


class YahooError(Exception):
    pass


def http_get(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


@dataclass
class Offer:
    """ヤフショの1商品（ストアごと）。price は税込。"""

    name: str
    price: int
    points: int = 0
    shipping_free: Optional[bool] = None   # None は不明
    shipping_label: str = ""
    store: str = ""
    store_rating: Optional[float] = None
    store_reviews: Optional[int] = None
    url: str = ""
    jan: str = ""
    condition: str = ""
    in_stock: bool = True


def build_url(appid: str, *, jan: str = "", query: str = "", price_from: Optional[int] = None,
              results: int = 30) -> str:
    params: Dict[str, Any] = {
        "appid": appid,
        "condition": "new",
        "in_stock": "true",
        "sort": "+price",
        "results": results,
    }
    if jan:
        params["jan_code"] = jan
    else:
        params["query"] = query
    if price_from:
        params["price_from"] = price_from
    return f"{API_URL}?{urllib.parse.urlencode(params)}"


def _int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def parse_hit(h: Dict[str, Any], premium: bool = False) -> Optional[Offer]:
    price = _int(h.get("price"))
    if premium and h.get("premiumPriceStatus") and _int(h.get("premiumPrice")) > 0:
        price = _int(h.get("premiumPrice"))
    if price <= 0 or not h.get("name"):
        return None
    pt = h.get("point") or {}
    keys = ["amount", "bonusAmount"] + (["premiumAmount", "premiumBonusAmount"] if premium else [])
    # 通常とボーナスが合計か上乗せかはAPIの表記だけでは断定できないので、大きい方（控えめな値）を使う
    points = max(_int(pt.get(k)) for k in keys)
    ship = h.get("shipping") or {}
    label = str(ship.get("name") or "")
    # 送料は表示名で判断する（「条件付き送料無料」もカメラの価格帯なら条件を満たすので無料扱い）。
    # 表示名がなければ不明として、設定の見込み送料を足す
    free: Optional[bool] = ("送料無料" in label) if label else None
    seller = h.get("seller") or {}
    review = seller.get("review") or {}
    rating = review.get("rate")
    count = review.get("count")
    return Offer(
        name=str(h.get("name")),
        price=price,
        points=points,
        shipping_free=free,
        shipping_label=label,
        store=str(seller.get("name") or ""),
        store_rating=float(rating) if isinstance(rating, (int, float)) else None,
        store_reviews=int(count) if isinstance(count, (int, float)) else None,
        url=str(h.get("url") or ""),
        jan=str(h.get("janCode") or ""),
        condition=str(h.get("condition") or ""),
        in_stock=h.get("inStock") is not False,
    )


def parse_response(payload: bytes, premium: bool = False) -> List[Offer]:
    data = json.loads(payload.decode("utf-8"))
    if isinstance(data, dict) and "Error" in data:
        raise YahooError(str((data.get("Error") or {}).get("Message") or data["Error"]))
    offers = []
    for h in data.get("hits") or []:
        if isinstance(h, dict):
            o = parse_hit(h, premium)
            if o is not None and o.in_stock and o.condition in ("", "new"):
                offers.append(o)
    return offers


def search(appid: str, *, jan: str = "", query: str = "", price_from: Optional[int] = None,
           results: int = 30, premium: bool = False, fetch: Fetcher = http_get) -> List[Offer]:
    url = build_url(appid, jan=jan, query=query, price_from=price_from, results=results)
    try:
        payload = fetch(url)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            body = json.loads(e.read().decode("utf-8"))
            detail = str((body.get("Error") or {}).get("Message") or "")
        except (ValueError, AttributeError, OSError):
            pass
        if e.code in (401, 403):
            raise YahooError(f"アプリケーションID（Client ID）が正しくないか、利用が許可されていません（HTTP {e.code}）"
                             f"{detail}") from None
        if e.code == 429:
            raise YahooError("短時間にアクセスしすぎました。時間をおいて実行してください（HTTP 429）") from None
        raise YahooError(f"HTTP {e.code} {detail}".strip()) from None
    except (urllib.error.URLError, OSError) as e:
        raise YahooError(f"接続できませんでした（{e}）") from None
    try:
        return parse_response(payload, premium)
    except (ValueError, AttributeError, TypeError) as e:
        raise YahooError(f"検索結果を読めませんでした（{e}）") from None
