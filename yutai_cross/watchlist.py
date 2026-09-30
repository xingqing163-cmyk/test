"""ウォッチリスト（優待銘柄の一覧）CSV の読み込み。

見出しは日本語・英語のどちらでもよい。Excel で保存した Shift_JIS(CP932) の CSV も読める。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# 見出し名 → 内部名
HEADER_ALIASES: Dict[str, str] = {
    "code": "code", "コード": "code", "銘柄コード": "code",
    "name": "name", "銘柄名": "name", "銘柄": "name",
    "record_month": "record_month", "権利月": "record_month",
    "record_day": "record_day", "権利日": "record_day", "基準日": "record_day",
    "shares": "shares", "必要株数": "shares", "株数": "shares",
    "price": "price", "株価": "price",
    "benefit_value": "benefit_value", "優待価値": "benefit_value", "優待価値(円)": "benefit_value",
    "resale_value": "resale_value", "換金価値": "resale_value", "換金価値(円)": "resale_value",
    "benefit": "benefit", "優待内容": "benefit",
    "dividend": "dividend", "1株配当": "dividend", "配当": "dividend",
    "taishaku": "taishaku", "貸借": "taishaku", "貸借銘柄": "taishaku",
    "ippan": "ippan", "一般信用": "ippan", "一般売り": "ippan",
    "long_term": "long_term", "長期条件": "long_term", "長期保有条件": "long_term",
    "popularity": "popularity", "人気": "popularity",
    "gyakuhibu_est": "gyakuhibu_est", "逆日歩想定": "gyakuhibu_est",
    "gyakuhibu_worst": "gyakuhibu_worst", "逆日歩最悪": "gyakuhibu_worst",
    "memo": "memo", "メモ": "memo",
    "enabled": "enabled", "有効": "enabled",
}

REQUIRED = ("code", "record_month", "benefit_value")

_TRUE = {"1", "true", "yes", "y", "○", "〇", "◯", "有", "あり", "可", "貸借"}
_FALSE = {"0", "false", "no", "n", "×", "✕", "無", "なし", "不可", "-", "－"}
_NONE_BROKERS = {"なし", "無", "-", "－", "×", "none", "no"}


@dataclass
class WatchItem:
    code: str
    name: str
    record_months: List[int]
    record_day: str = "末"
    shares: int = 100
    price: Optional[float] = None
    benefit_value: int = 0
    resale_value: Optional[int] = None
    benefit: str = ""
    dividend: float = 0.0            # 1株あたり配当（その権利日の分）
    taishaku: bool = False           # 貸借銘柄（制度信用で売れる）
    ippan_brokers: Optional[List[str]] = None  # None=指定なし（全社を候補）、[]=一般売り不可
    long_term: str = ""
    popularity: str = ""
    gyakuhibu_est: Optional[float] = None    # 円/株/日
    gyakuhibu_worst: Optional[float] = None  # 円/株/日
    memo: str = ""
    enabled: bool = True
    line_no: int = 0
    notes: List[str] = field(default_factory=list)

    def value(self, valuation: str) -> int:
        if valuation == "resale" and self.resale_value is not None:
            return self.resale_value
        return self.benefit_value


def _to_bool(text: str, default: bool) -> bool:
    t = text.strip().lower()
    if not t:
        return default
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    raise ValueError(f"はい/いいえ を判別できません: {text!r}")


def _to_number(text: str) -> Optional[float]:
    t = text.strip().replace(",", "").replace("円", "").replace("¥", "")
    if not t:
        return None
    return float(t)


def _parse_months(text: str) -> List[int]:
    parts = [p for p in re.split(r"[,;/・、\s]+", text.replace("月", " ")) if p]
    months = sorted({int(p) for p in parts})
    for m in months:
        if not 1 <= m <= 12:
            raise ValueError(f"権利月が不正です: {text!r}")
    if not months:
        raise ValueError("権利月が空です")
    return months


def _parse_brokers(text: str) -> Optional[List[str]]:
    t = text.strip()
    if not t:
        return None
    if t.lower() in _NONE_BROKERS:
        return []
    return [p for p in re.split(r"[,;/・、\s]+", t) if p]


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path}: 文字コードを判別できません（UTF-8 か Shift_JIS で保存してください）")


def parse_watchlist(text: str, source: str = "watchlist") -> List[WatchItem]:
    rows = list(csv.reader(io.StringIO(text)))
    # 空行と # で始まるコメント行を除く
    rows = [r for r in rows if any(c.strip() for c in r) and not r[0].lstrip().startswith("#")]
    if not rows:
        return []
    header = [HEADER_ALIASES.get(h.strip(), h.strip()) for h in rows[0]]
    missing = [k for k in REQUIRED if k not in header]
    if missing:
        raise ValueError(f"{source}: 必須の列がありません: {missing}")

    items: List[WatchItem] = []
    for i, row in enumerate(rows[1:], start=2):
        rec = {header[j]: (row[j] if j < len(row) else "") for j in range(len(header))}
        try:
            items.append(_item_from_record(rec, i))
        except (ValueError, KeyError) as e:
            raise ValueError(f"{source} {i}行目: {e}") from None
    return items


def _item_from_record(rec: Dict[str, str], line_no: int) -> WatchItem:
    def get(key: str) -> str:
        return (rec.get(key) or "").strip()

    code = get("code")
    if not code:
        raise ValueError("コードが空です")
    benefit_value = _to_number(get("benefit_value"))
    if benefit_value is None:
        raise ValueError("優待価値が空です")
    resale = _to_number(get("resale_value"))
    shares = _to_number(get("shares"))
    return WatchItem(
        code=code,
        name=get("name") or code,
        record_months=_parse_months(get("record_month")),
        record_day=get("record_day") or "末",
        shares=int(shares) if shares else 100,
        price=_to_number(get("price")),
        benefit_value=int(benefit_value),
        resale_value=int(resale) if resale is not None else None,
        benefit=get("benefit"),
        dividend=_to_number(get("dividend")) or 0.0,
        taishaku=_to_bool(get("taishaku"), False),
        ippan_brokers=_parse_brokers(get("ippan")),
        long_term=get("long_term"),
        popularity=get("popularity"),
        gyakuhibu_est=_to_number(get("gyakuhibu_est")),
        gyakuhibu_worst=_to_number(get("gyakuhibu_worst")),
        memo=get("memo"),
        enabled=_to_bool(get("enabled"), True),
        line_no=line_no,
    )


def load_watchlist(path: Path) -> List[WatchItem]:
    return parse_watchlist(_read_text(Path(path)), source=str(path))


def load_price_csv(path: Path) -> Dict[str, float]:
    """「コード,株価」の CSV を読み込む（証券会社からエクスポートした株価などを使う用）。"""
    rows = list(csv.reader(io.StringIO(_read_text(Path(path)))))
    prices: Dict[str, float] = {}
    for row in rows:
        if len(row) < 2 or not row[0].strip() or row[0].lstrip().startswith("#"):
            continue
        try:
            value = _to_number(row[1])
        except ValueError:
            continue  # 見出し行など
        if value is not None:
            prices[row[0].strip()] = value
    return prices
