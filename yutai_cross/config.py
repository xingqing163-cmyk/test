"""設定（証券会社ごとの売建方法・料率、判定しきい値など）。

設定は JSON ファイルで上書きできる。値は 2026年9月時点で公表されていた条件を
もとにした初期値なので、実際に使う前に必ず各証券会社の最新の料率を確認すること。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from .jpx_calendar import DEFAULT_SETTLEMENT_DAYS

# 上場株式の配当にかかる源泉税（所得税15.315%＋住民税5%）
DIVIDEND_TAX_RATE = 0.20315
# 制度信用の売方が支払う配当落調整金（配当の税引後相当額）
SEIDO_DIVIDEND_ADJ_RATE = 0.84685

KIND_IPPAN = "ippan"  # 一般信用
KIND_SEIDO = "seido"  # 制度信用


@dataclass
class ShortMethod:
    """売建（空売り）の方法。証券会社×信用区分×返済期限ごとに1つ。"""

    id: str
    broker: str
    label: str
    kind: str = KIND_IPPAN
    lending_rate: float = 0.039            # 貸株料（年率）
    max_hold_business_days: Optional[int] = None  # 返済期限（営業日）。None は無期限
    day_count: str = "both"                # 貸株料の日数: both=両端入れ / one=片端入れ
    commission_buy: int = 0                # 現物買いの手数料（円）
    commission_short: int = 0              # 信用新規売りの手数料（円）。現渡しは無料が一般的
    dividend_adj_rate: Optional[float] = None  # 売方が払う配当落調整金の割合（省略時は区分から自動）
    enabled: bool = True

    @property
    def is_seido(self) -> bool:
        return self.kind == KIND_SEIDO

    @property
    def adj_rate(self) -> float:
        if self.dividend_adj_rate is not None:
            return self.dividend_adj_rate
        return SEIDO_DIVIDEND_ADJ_RATE if self.is_seido else 1.0

    @property
    def term_label(self) -> str:
        if self.is_seido:
            return "制度(6ヶ月)"
        if self.max_hold_business_days is None:
            return "無期限"
        return f"短期({self.max_hold_business_days}営業日)"


def default_methods() -> List[ShortMethod]:
    """初期値。2026年9月時点の公表値を参考にした例（要確認）。"""
    return [
        ShortMethod(
            id="sbi_short", broker="SBI", label="SBI証券 一般信用(短期)",
            lending_rate=0.039, max_hold_business_days=15,
        ),
        ShortMethod(
            id="sbi_long", broker="SBI", label="SBI証券 一般信用(無期限)",
            lending_rate=0.011, max_hold_business_days=None,
        ),
        ShortMethod(
            id="rakuten_short", broker="楽天", label="楽天証券 一般信用(短期)",
            lending_rate=0.039, max_hold_business_days=14,
        ),
        ShortMethod(
            id="rakuten_long", broker="楽天", label="楽天証券 一般信用(無期限)",
            lending_rate=0.011, max_hold_business_days=None,
        ),
        ShortMethod(
            id="seido", broker="制度", label="制度信用（逆日歩リスクあり）",
            kind=KIND_SEIDO, lending_rate=0.0115, max_hold_business_days=None,
        ),
    ]


@dataclass
class Settings:
    methods: List[ShortMethod] = field(default_factory=default_methods)
    # 優待の評価: benefit=自分で使う場合の価値 / resale=金券ショップ等で換金した場合の価値
    valuation: str = "benefit"
    # これ未満の利益しか出ない銘柄は「見送り」
    min_profit_yen: int = 300
    # 今日から何日先の権利付最終日までを対象にするか
    horizon_days: int = 60
    # 在庫確保のために「最安時の利益」の何割までを貸株料の上乗せに使ってよいか
    inventory_premium_ratio: float = 0.3
    # 人気度（ウォッチリストの「人気」列）ごとの上乗せ許容割合
    popularity_premium: Dict[str, float] = field(
        default_factory=lambda: {"高": 0.5, "中": 0.3, "低": 0.0}
    )
    # 無期限の一般信用で、権利付最終日の何営業日前まで先回りを検討するか
    # （早く建てるほど資金が拘束され、優待変更・追証などのリスクにさらされる期間も延びる）
    max_lookback_business_days: int = 10
    # 特定口座(源泉徴収あり)＋株式数比例配分方式で、配当の税金が損益通算で戻るか
    dividend_tax_recovered: bool = True
    # 優待(雑所得)にかかる税率の目安（所得税＋住民税の限界税率）。0なら税引前で評価
    benefit_tax_rate: float = 0.0
    # 信用建玉に必要な委託保証金率
    margin_rate: float = 0.30
    # 制度信用の判定に使う逆日歩: worst=最悪ケース / expected=想定値
    seido_risk_basis: str = "worst"
    # 権利付最終日の最高料率の倍率（日証金ルール: 権利付最終売買日は4倍。異常時は10倍もあり得る）
    seido_worst_multiplier: float = 4.0
    settlement_days: int = DEFAULT_SETTLEMENT_DAYS
    extra_market_holidays: List[date] = field(default_factory=list)
    # 勤務先・取引先など、社内規程やインサイダー規制で売買しない銘柄コード
    exclude_codes: List[str] = field(default_factory=list)
    # 長期保有条件のある銘柄も推奨に含めるか（毎回クロスして株主番号をつなぐ「継続クロス」をする人向け）
    include_long_term: bool = False

    def enabled_methods(self) -> List[ShortMethod]:
        return [m for m in self.methods if m.enabled]

    def premium_ratio_for(self, popularity: str) -> float:
        return self.popularity_premium.get(popularity, self.inventory_premium_ratio)


def _strip_comments(d: Dict[str, Any]) -> Dict[str, Any]:
    """"_" で始まるキー（JSON内のコメント用）を除く。"""
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _method_from_dict(d: Dict[str, Any]) -> ShortMethod:
    d = _strip_comments(d)
    known = {f.name for f in fields(ShortMethod)}
    unknown = set(d) - known
    if unknown:
        raise ValueError(f"methods の未知の項目: {sorted(unknown)}")
    return ShortMethod(**d)


def settings_from_dict(data: Dict[str, Any]) -> Settings:
    data = _strip_comments(data)
    kwargs: Dict[str, Any] = {}
    if "methods" in data:
        kwargs["methods"] = [_method_from_dict(m) for m in data.pop("methods")]
    if "extra_market_holidays" in data:
        kwargs["extra_market_holidays"] = [
            date.fromisoformat(s) for s in data.pop("extra_market_holidays")
        ]
    if "exclude_codes" in data:
        kwargs["exclude_codes"] = [str(c) for c in data.pop("exclude_codes")]
    known = {f.name for f in fields(Settings)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"設定ファイルの未知の項目: {sorted(unknown)}")
    kwargs.update(data)
    s = Settings(**kwargs)
    if s.valuation not in ("benefit", "resale"):
        raise ValueError("valuation は benefit か resale を指定してください")
    if s.seido_risk_basis not in ("worst", "expected"):
        raise ValueError("seido_risk_basis は worst か expected を指定してください")
    ids = [m.id for m in s.methods]
    if len(ids) != len(set(ids)):
        raise ValueError("methods の id が重複しています")
    for m in s.methods:
        if m.kind not in (KIND_IPPAN, KIND_SEIDO):
            raise ValueError(f"{m.id}: kind は ippan か seido を指定してください")
        if m.day_count not in ("both", "one"):
            raise ValueError(f"{m.id}: day_count は both か one を指定してください")
    return s


def load_settings(path: Optional[Path]) -> Settings:
    if path is None:
        return Settings()
    with open(path, encoding="utf-8") as f:
        return settings_from_dict(json.load(f))
