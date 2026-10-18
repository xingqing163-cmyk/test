"""設定（ヤフショのポイント・送料・消費税の扱い、判定しきい値、買取サイトの一覧）。

設定は JSON ファイルで上書きできる。ポイント倍率や送料は人によって違うので、
自分の条件（LYPプレミアム会員か、PayPayカードで払うか など）に合わせて書き換えること。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 消費税の扱い
TAX_EXEMPT = "exempt"          # 免税事業者（消費税を納めない）
TAX_GENERAL = "general"        # 本則課税（売上の消費税 − 仕入の消費税）
TAX_SIMPLIFIED = "simplified"  # 簡易課税（売上の消費税 ×（1 − みなし仕入率））
TAX_SPECIAL = "special"        # 2割特例など（売上の消費税 × 一定割合）
TAX_MODES = (TAX_EXEMPT, TAX_GENERAL, TAX_SIMPLIFIED, TAX_SPECIAL)
TAX_MODE_LABELS = {
    TAX_EXEMPT: "免税事業者",
    TAX_GENERAL: "本則課税",
    TAX_SIMPLIFIED: "簡易課税",
    TAX_SPECIAL: "特例（売上税額×割合）",
}

# ヤフショの商品名にこの言葉があれば仕入れ対象から外す（新品の買取価格と比べられないもの）
DEFAULT_EXCLUDE_WORDS = [
    "中古", "訳あり", "訳アリ", "わけあり", "アウトレット", "展示品", "展示処分", "開封品", "開封済",
    "再生品", "整備済", "リファービッシュ", "ジャンク", "箱なし", "箱無し", "外箱なし", "保証なし",
    "保証書なし", "並行輸入", "海外版", "海外仕様", "輸入品", "レンタル",
]
# 外さないが注意を出す言葉
DEFAULT_CAUTION_WORDS = [
    "セット", "予約", "取り寄せ", "取寄", "入荷待ち", "納期", "受注生産", "箱傷", "箱潰れ",
]


@dataclass
class Site:
    """買取サイト（カテゴリごとに1つ）。"""

    name: str
    url: str
    category: str = ""
    enabled: bool = True


def default_sites() -> List[Site]:
    return [
        Site(name="SY買取", category="カメラ",
             url="https://www.sy-kaitori.com/products?category=cmt2jn04n001l01nvwg3whuml"),
    ]


@dataclass
class Settings:
    # Yahoo!デベロッパーネットワークで発行した Client ID（アプリケーションID）。
    # 空なら環境変数 YAHOO_APPID を使う
    yahoo_appid: str = ""
    # LYPプレミアム会員か（会員価格・会員向けポイントがあればそちらで計算）
    lyp_premium: bool = False
    # APIの表示ポイントに上乗せする自分のポイント率（税抜価格に対する割合）。
    # 例: PayPayカード払い・5のつく日など、APIに出てこない分。不確かなら 0 のまま（控えめに判定）
    extra_point_rate: float = 0.0
    # 1ポイントを何円とみなすか（期間限定ポイントを使い切れないなら 1 未満に）
    point_value: float = 1.0
    # ヤフショ側が「送料無料」でないときに見込む送料（円）
    assumed_shipping_yen: int = 1000
    # 買取サイトへ送る送料（1点あたり・円）。買取側が送料負担なら 0
    send_shipping_yen: int = 0
    # 振込手数料（1点あたり・円）。まとめて売るなら点数で割った額
    transfer_fee_yen: int = 0
    # 梱包材などその他の経費（1点あたり・円）
    other_cost_yen: int = 0
    # 消費税の扱い: exempt / general / simplified / special
    tax_mode: str = TAX_EXEMPT
    # 簡易課税のみなし仕入率。事業者（買取業者）への販売は第1種事業（卸売業）で 0.9
    deemed_purchase_rate: float = 0.9
    # special のとき、売上の消費税のうち納める割合（2割特例なら 0.2）
    special_ratio: float = 0.2
    # 本則課税で、仕入の消費税のうち控除できる割合（インボイス未登録ストアからの仕入れが多いなら下げる）
    input_credit_ratio: float = 1.0
    # 黒字と判定する最低利益（円）と最低利益率（仕入額に対する割合）
    min_profit_yen: int = 1000
    min_profit_rate: float = 0.02
    # 判定にポイントを含めるか（false なら現金だけで判定）
    judge_with_points: bool = True
    # 名前で探したとき、買取価格のこの割合より安い商品は別商品・付属品とみなして除外
    price_floor_ratio: float = 0.6
    exclude_words: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_WORDS))
    caution_words: List[str] = field(default_factory=lambda: list(DEFAULT_CAUTION_WORDS))
    # ストア評価がこれ未満（件数・平均）なら注意を出す
    min_store_reviews: int = 30
    min_store_rating: float = 4.0
    # 1商品あたりに取得するヤフショの検索結果の件数
    yahoo_results: int = 30
    # ヤフショAPIを呼ぶ間隔（秒）
    yahoo_interval_sec: float = 1.0
    # 買取サイトのページを取る間隔（秒）と最大ページ数
    site_interval_sec: float = 2.0
    max_pages: int = 30
    sites: List[Site] = field(default_factory=default_sites)

    def appid(self) -> str:
        return self.yahoo_appid.strip() or os.environ.get("YAHOO_APPID", "").strip()

    def enabled_sites(self) -> List[Site]:
        return [s for s in self.sites if s.enabled]


_NUM: Tuple[type, ...] = (int, float)
_SETTINGS_TYPES: Dict[str, Tuple[type, ...]] = {
    "yahoo_appid": (str,), "lyp_premium": (bool,), "extra_point_rate": _NUM, "point_value": _NUM,
    "assumed_shipping_yen": (int,), "send_shipping_yen": (int,), "transfer_fee_yen": (int,),
    "other_cost_yen": (int,), "tax_mode": (str,), "deemed_purchase_rate": _NUM,
    "special_ratio": _NUM, "input_credit_ratio": _NUM, "min_profit_yen": (int,),
    "min_profit_rate": _NUM, "judge_with_points": (bool,), "price_floor_ratio": _NUM,
    "exclude_words": (list,), "caution_words": (list,), "min_store_reviews": (int,),
    "min_store_rating": _NUM, "yahoo_results": (int,), "yahoo_interval_sec": _NUM,
    "site_interval_sec": _NUM, "max_pages": (int,),
}
_SITE_TYPES: Dict[str, Tuple[type, ...]] = {
    "name": (str,), "url": (str,), "category": (str,), "enabled": (bool,),
}


def _check_types(obj: Any, spec: Dict[str, Tuple[type, ...]], where: str) -> None:
    for name, types in spec.items():
        v = getattr(obj, name)
        # True/False は int の一種なので、bool を許さない項目では弾く
        if not isinstance(v, types) or (isinstance(v, bool) and bool not in types):
            raise ValueError(f"{where}{name} の値の型が正しくありません: {v!r}")


def _strip_comments(d: Dict[str, Any]) -> Dict[str, Any]:
    """"_" で始まるキー（JSON内のコメント用）を除く。"""
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _site_from_dict(d: Any) -> Site:
    if not isinstance(d, dict):
        raise ValueError("sites の各要素は { ... } の形で書いてください")
    d = _strip_comments(d)
    missing = {"name", "url"} - set(d)
    if missing:
        raise ValueError(f"sites に必須の項目がありません: {sorted(missing)}")
    unknown = set(d) - {f.name for f in fields(Site)}
    if unknown:
        raise ValueError(f"sites の未知の項目: {sorted(unknown)}")
    site = Site(**d)
    _check_types(site, _SITE_TYPES, f"sites[{site.name}].")
    if not site.url.startswith(("https://", "http://")):
        raise ValueError(f"sites[{site.name}].url は https:// で始まるURLにしてください")
    return site


def settings_from_dict(data: Dict[str, Any]) -> Settings:
    data = _strip_comments(data)
    kwargs: Dict[str, Any] = {}
    if "sites" in data:
        sites = data.pop("sites")
        if not isinstance(sites, list):
            raise ValueError("sites は [ ... ] の形で書いてください")
        kwargs["sites"] = [_site_from_dict(s) for s in sites]
    unknown = set(data) - {f.name for f in fields(Settings)}
    if unknown:
        raise ValueError(f"設定ファイルの未知の項目: {sorted(unknown)}")
    kwargs.update(data)
    s = Settings(**kwargs)
    _check_types(s, _SETTINGS_TYPES, "")
    if s.tax_mode not in TAX_MODES:
        raise ValueError("tax_mode は exempt / general / simplified / special のどれかを指定してください")
    for name in ("extra_point_rate", "deemed_purchase_rate", "special_ratio", "input_credit_ratio",
                 "min_profit_rate", "price_floor_ratio"):
        if not 0 <= getattr(s, name) <= 1:
            raise ValueError(f"{name} は 0〜1 の割合で指定してください（例: 5% なら 0.05）")
    for name in ("assumed_shipping_yen", "send_shipping_yen", "transfer_fee_yen", "other_cost_yen",
                 "min_store_reviews"):
        if getattr(s, name) < 0:
            raise ValueError(f"{name} は 0 以上にしてください")
    if not 0 <= s.point_value <= 1.5:
        raise ValueError("point_value は 1ポイントあたりの円（例: 1.0）で指定してください")
    if not 1 <= s.yahoo_results <= 50:
        raise ValueError("yahoo_results は 1〜50 にしてください")
    if s.yahoo_interval_sec < 0.5 or s.site_interval_sec < 1:
        raise ValueError("相手のサーバーに負担をかけないよう、yahoo_interval_sec は0.5秒以上、"
                         "site_interval_sec は1秒以上にしてください")
    if s.max_pages < 1:
        raise ValueError("max_pages は1以上にしてください")
    for name in ("exclude_words", "caution_words"):
        if not all(isinstance(w, str) for w in getattr(s, name)):
            raise ValueError(f"{name} は文字列のリストにしてください")
    return s


def load_settings(path: Optional[Path]) -> Settings:
    if path is None:
        return Settings()
    try:
        # Windows のメモ帳が付ける BOM も読めるように utf-8-sig
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"{path} の {e.lineno}行目 {e.colno}文字目付近の書き方が正しくありません"
            "（カンマの付け忘れや、最後の要素の後ろの余計なカンマが多い原因です）"
        ) from None
    except UnicodeDecodeError:
        raise ValueError(f"{path}: 文字コードを UTF-8 にして保存してください") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 設定は {{ ... }} の形で書いてください")
    try:
        return settings_from_dict(data)
    except (TypeError, ValueError, AttributeError) as e:
        raise ValueError(f"{path}: {e}") from None
