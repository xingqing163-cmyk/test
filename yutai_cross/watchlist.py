"""ウォッチリスト（優待銘柄の一覧）CSV の読み込み。

見出しは日本語・英語のどちらでもよい。Excel で保存した Shift_JIS(CP932) の CSV も読める。
全角の数字・英字、「1,850円」「￥1,850」のような書き方も受け付ける。
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import normalize_code

# 見出し名 → 内部名（全角・半角の違いは読み込み時に吸収する。末尾の「(円)」は無視）
HEADER_ALIASES: Dict[str, str] = {
    "code": "code", "コード": "code", "銘柄コード": "code",
    "name": "name", "銘柄名": "name", "銘柄": "name",
    "record_month": "record_month", "権利月": "record_month",
    "record_day": "record_day", "権利日": "record_day", "基準日": "record_day",
    "shares": "shares", "必要株数": "shares", "株数": "shares",
    "price": "price", "株価": "price",
    "benefit_value": "benefit_value", "優待価値": "benefit_value",
    "resale_value": "resale_value", "換金価値": "resale_value",
    "benefit": "benefit", "優待内容": "benefit",
    "dividend": "dividend", "1株配当": "dividend", "配当": "dividend",
    "taishaku": "taishaku", "貸借": "taishaku", "貸借銘柄": "taishaku",
    "ippan": "ippan", "一般信用": "ippan", "一般売り": "ippan",
    "long_term": "long_term", "長期条件": "long_term", "長期保有条件": "long_term",
    "popularity": "popularity", "人気": "popularity",
    "gyakuhibu_est": "gyakuhibu_est", "逆日歩想定": "gyakuhibu_est",
    "gyakuhibu_worst": "gyakuhibu_worst", "逆日歩最悪": "gyakuhibu_worst",
    "crossed": "crossed", "クロス済": "crossed", "クロス済み": "crossed",
    "memo": "memo", "メモ": "memo",
    "enabled": "enabled", "有効": "enabled",
}

REQUIRED = {"code": "コード", "record_month": "権利月", "benefit_value": "優待価値"}

_TRUE = {"1", "true", "yes", "y", "○", "〇", "◯", "有", "あり", "可", "貸借", "済"}
_FALSE = {"0", "false", "no", "n", "×", "✕", "無", "なし", "不可", "-", "−", "ー"}
_NONE_BROKERS = {"なし", "無", "-", "−", "ー", "×", "✕", "none", "no", "不可"}
_ALL_BROKERS = {"○", "〇", "◯", "あり", "有", "可", "全部", "全社", "all"}
_TERMS = ("短期", "無期限", "長期")
_NUM_NONE = {"", "-", "−", "ー", "―", "なし", "無", "n/a", "na"}
_MONTH_END = {"", "末", "末日", "月末", "月末日", "end", "last"}
_POPULARITY = {
    "高": "高", "high": "高", "h": "高", "大": "高",
    "中": "中", "mid": "中", "m": "中", "普通": "中",
    "低": "低", "low": "低", "l": "低", "小": "低",
}


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
    crossed: bool = False            # すでにクロス済み（エントリーの通知を止める）
    memo: str = ""
    enabled: bool = True
    line_no: int = 0
    notes: List[str] = field(default_factory=list)  # 読み込み時の軽い注意（処理は続ける）

    def value(self, valuation: str) -> int:
        if valuation == "resale" and self.resale_value is not None:
            return self.resale_value
        return self.benefit_value

    @property
    def tax_base(self) -> int:
        """税の目安計算に使う額。確定申告では受取時の時価なので、主観で割り引いた額より大きい方を使う。"""
        return max(self.benefit_value, self.resale_value or 0)


def nfkc(text: str) -> str:
    """全角英数字・記号を半角にそろえる。"""
    return unicodedata.normalize("NFKC", text or "").strip()


def csv_rows(text: str) -> List[Tuple[int, List[str]]]:
    """空行と # のコメント行を除いた行を、(Excel上の行番号, 列) で返す。改行が CR だけのファイルも読める。"""
    reader = csv.reader(io.StringIO(text.lstrip("﻿"), newline=""))
    rows = []
    for row in reader:
        if any(c.strip() for c in row) and not row[0].lstrip().startswith(("#", "＃")):
            rows.append((reader.line_num, row))
    return rows


def _norm_header(h: str) -> str:
    h = nfkc(h)
    if h in HEADER_ALIASES:
        return HEADER_ALIASES[h]
    base = re.sub(r"\(.*?\)$", "", h).strip()  # 「1株配当(円)」→「1株配当」
    return HEADER_ALIASES.get(base, h)


def _to_bool(text: str, default: bool) -> bool:
    t = nfkc(text).lower()
    if not t:
        return default
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    raise ValueError(f"○か×で書いてください: {text!r}")


def to_number(text: str) -> Optional[float]:
    t = re.sub(r"[,\s円¥\\]", "", nfkc(text))  # CP932 の ¥ は "\" として読まれる
    if t.lower() in _NUM_NONE:
        return None
    try:
        return float(t)
    except ValueError:
        raise ValueError(f"数字として読めません: {text!r}（例: 1000。「相当」などの文字は入れない）") from None


def _parse_months(text: str) -> List[int]:
    t = nfkc(text)
    parts = [p for p in re.split(r"[,;/・、\s]+", t.replace("月", " ")) if p]
    try:
        months = sorted({int(p) for p in parts})
    except ValueError:
        raise ValueError(
            f"権利月が読めません: {text!r}（例: 3、3・9。Excelが日付に変えた場合は「3・9」と書き直してください）"
        ) from None
    if not months:
        raise ValueError("権利月が空です")
    for m in months:
        if not 1 <= m <= 12:
            raise ValueError(f"権利月は1〜12で書いてください: {text!r}")
    return months


def _parse_record_day(text: str) -> str:
    t = nfkc(text).lower()
    if t in _MONTH_END:
        return "末"
    if t.endswith("日"):
        t = t[:-1]
    if not t.isdigit() or not 1 <= int(t) <= 31:
        raise ValueError(f"権利日は「末」か 1〜31 の日付で書いてください: {text!r}")
    return str(int(t))


def _parse_brokers(text: str) -> Optional[List[str]]:
    """「一般信用」列を証券会社（＋期限）のリストにする。None=全社を候補、[]=一般信用不可。"""
    t = nfkc(text)
    if not t or t.lower() in _ALL_BROKERS:
        return None
    if t.lower() in _NONE_BROKERS:
        return []
    t = re.sub(r"一般信用|一般", " ", t).replace("証券", "")
    t = re.sub(r"[()\[\]「」]", " ", t)
    tokens: List[str] = []
    for tok in (p for p in re.split(r"[,;/・、\s]+", t) if p):
        if tok in _TERMS and tokens:
            tokens[-1] += tok  # 「SBI 短期」→「SBI短期」
        else:
            tokens.append(tok)
    return tokens


def read_text(path: Path) -> str:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path}: 文字コードを判別できません（UTF-8 か Shift_JIS で保存してください）")


def parse_watchlist(text: str, source: str = "watchlist",
                    errors: Optional[List[str]] = None) -> List[WatchItem]:
    """errors にリストを渡すと、読めない行で止まらずにエラーを集めて残りを読む（入力チェック用）。"""
    rows = csv_rows(text)
    if not rows:
        return []
    header = [_norm_header(h) for h in rows[0][1]]
    missing = [label for key, label in REQUIRED.items() if key not in header]
    if missing:
        raise ValueError(
            f"{source}: 必須の列がありません: {'、'.join(missing)}"
            "（1行目の見出しの書き方は examples/watchlist_sample.csv を参照）"
        )

    items: List[WatchItem] = []
    for line_no, row in rows[1:]:
        rec = {header[j]: (row[j] if j < len(row) else "") for j in range(len(header))}
        try:
            items.append(_item_from_record(rec, line_no))
        except (ValueError, KeyError) as e:
            message = f"{source} の {line_no}行目（Excelの行番号）: {e}"
            if errors is None:
                raise ValueError(message) from None
            errors.append(message)
    return items


def _item_from_record(rec: Dict[str, str], line_no: int) -> WatchItem:
    def get(key: str) -> str:
        return (rec.get(key) or "").strip()

    code = normalize_code(get("code"))
    if not code:
        raise ValueError("コードが空です")
    benefit_value = to_number(get("benefit_value"))
    if benefit_value is None:
        raise ValueError("優待価値が空です")
    resale = to_number(get("resale_value"))
    shares = to_number(get("shares"))
    if shares is not None and (shares <= 0 or shares != int(shares)):
        raise ValueError(f"必要株数は正の整数で書いてください: {get('shares')!r}")

    notes: List[str] = []
    pop_raw = nfkc(get("popularity")).lower()
    popularity = _POPULARITY.get(pop_raw, "")
    if pop_raw and not popularity:
        notes.append(f"「人気」列の {get('popularity')!r} は読めないので「中」として扱いました（高・中・低のどれか）")

    return WatchItem(
        code=code,
        name=get("name") or code,
        record_months=_parse_months(get("record_month")),
        record_day=_parse_record_day(get("record_day")),
        shares=int(shares) if shares else 100,
        price=to_number(get("price")),
        benefit_value=int(benefit_value),
        resale_value=int(resale) if resale is not None else None,
        benefit=get("benefit"),
        dividend=to_number(get("dividend")) or 0.0,
        taishaku=_to_bool(get("taishaku"), False),
        ippan_brokers=_parse_brokers(get("ippan")),
        long_term=get("long_term"),
        popularity=popularity,
        gyakuhibu_est=to_number(get("gyakuhibu_est")),
        gyakuhibu_worst=to_number(get("gyakuhibu_worst")),
        crossed=_to_bool(get("crossed"), False),
        memo=get("memo"),
        enabled=_to_bool(get("enabled"), True),
        line_no=line_no,
        notes=notes,
    )


def load_watchlist(path: Path, errors: Optional[List[str]] = None) -> List[WatchItem]:
    return parse_watchlist(read_text(Path(path)), source=str(path), errors=errors)


def load_price_csv(path: Path) -> Dict[str, float]:
    """「コード,株価」の CSV を読み込む（証券会社からエクスポートした株価などを使う用）。"""
    prices: Dict[str, float] = {}
    for _, row in csv_rows(read_text(Path(path))):
        if len(row) < 2 or not row[0].strip():
            continue
        try:
            value = to_number(row[1])
        except ValueError:
            continue  # 見出し行など
        if value is not None and value > 0:
            prices[normalize_code(row[0])] = value
    return prices
