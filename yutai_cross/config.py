"""設定（証券会社ごとの売建方法・料率、判定しきい値など）。

設定は JSON ファイルで上書きできる。値は 2026年9月時点で公表されていた条件を
もとにした初期値なので、実際に使う前に必ず各証券会社の最新の料率を確認すること。
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field, fields
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .jpx_calendar import DEFAULT_SETTLEMENT_DAYS

# 上場株式の配当にかかる源泉税（所得税15.315%〔復興特別所得税を含む。2037年分まで〕＋住民税5%）
DIVIDEND_TAX_RATE = 0.20315
# 制度信用の売方が支払う配当落調整金（配当から所得税相当額15.315%を控除した額。住民税5%分は控除しない）
SEIDO_DIVIDEND_ADJ_RATE = 0.84685

KIND_IPPAN = "ippan"  # 一般信用
KIND_SEIDO = "seido"  # 制度信用

BASIS_BUSINESS = "business"  # 返済期限を営業日で数える（例: SBI証券「15営業日」）
BASIS_CALENDAR = "calendar"  # 返済期限を暦日で数える（例: 楽天証券「14日」。期日が休日なら前営業日）


@dataclass
class ShortMethod:
    """売建（空売り）の方法。証券会社×信用区分×返済期限ごとに1つ。"""

    id: str
    broker: str
    label: str
    kind: str = KIND_IPPAN
    lending_rate: float = 0.039            # 貸株料（年率）
    max_hold_days: Optional[int] = None    # 返済期限の日数。None は無期限
    hold_basis: str = BASIS_BUSINESS       # max_hold_days の数え方: business / calendar
    day_count: str = "both"                # 貸株料の日数: both=両端入れ / one=片端入れ
    commission_buy: int = 0                # 現物買いの手数料（円）
    commission_short: int = 0              # 信用新規売りの手数料（円）。現渡しは無料が一般的
    dividend_adj_rate: Optional[float] = None  # 売方が払う配当落調整金の割合（省略時は区分から自動）
    enabled: bool = True

    @property
    def is_seido(self) -> bool:
        return self.kind == KIND_SEIDO

    @property
    def is_short_term(self) -> bool:
        """返済期限つきの一般信用（短期）か。"""
        return not self.is_seido and self.max_hold_days is not None

    @property
    def adj_rate(self) -> float:
        if self.dividend_adj_rate is not None:
            return self.dividend_adj_rate
        return SEIDO_DIVIDEND_ADJ_RATE if self.is_seido else 1.0

    @property
    def term_label(self) -> str:
        if self.is_seido:
            return "制度(6ヶ月)"
        if self.max_hold_days is None:
            return "無期限"
        unit = "日" if self.hold_basis == BASIS_CALENDAR else "営業日"
        return f"短期({self.max_hold_days}{unit})"


def default_methods() -> List[ShortMethod]:
    """初期値。2026年9月時点の公表値を参考にした例（要確認）。"""
    return [
        ShortMethod(
            id="sbi_short", broker="SBI", label="SBI証券 一般信用(短期)",
            lending_rate=0.039, max_hold_days=15, hold_basis=BASIS_BUSINESS,
        ),
        ShortMethod(
            id="sbi_long", broker="SBI", label="SBI証券 一般信用(無期限)",
            lending_rate=0.011,
        ),
        ShortMethod(
            id="rakuten_short", broker="楽天", label="楽天証券 一般信用(短期)",
            lending_rate=0.039, max_hold_days=14, hold_basis=BASIS_CALENDAR,
        ),
        ShortMethod(
            id="rakuten_long", broker="楽天", label="楽天証券 一般信用(無期限)",
            lending_rate=0.011,
        ),
        ShortMethod(
            id="seido", broker="制度", label="制度信用（逆日歩リスクあり）",
            kind=KIND_SEIDO, lending_rate=0.0115,
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
    # 特定口座(源泉徴収あり)＋株式数比例配分方式＋特定口座への配当受入で、配当の税金が損益通算で戻るか
    dividend_tax_recovered: bool = True
    # 優待(雑所得)にかかる税率の目安（所得税・復興特別所得税・住民税の限界税率。例: 20.42%+10% → 0.3042）
    benefit_tax_rate: float = 0.0
    # 信用建玉に必要な委託保証金率
    margin_rate: float = 0.30
    # 制度信用の判定に使う逆日歩: worst=最高料率×倍率で見積もる / expected=想定値（未入力なら worst と同じ）
    seido_risk_basis: str = "worst"
    # 権利付最終日の最高料率の倍率（日証金ルール: 権利付最終売買日は4倍。臨時措置で10倍もあり得る）
    seido_worst_multiplier: float = 4.0
    settlement_days: int = DEFAULT_SETTLEMENT_DAYS
    extra_market_holidays: List[date] = field(default_factory=list)
    # 勤務先・取引先など、社内規程やインサイダー規制で売買しない銘柄コード（入力ミス防止の補助）
    exclude_codes: List[str] = field(default_factory=list)
    # 長期保有条件のある銘柄も候補に含めるか（毎回クロスして株主番号をつなぐ「継続クロス」をする人向け）
    include_long_term: bool = False
    # 優待クロスに使える資金の上限（円）。0 なら上限なし。超える分は利回りの低い候補から見送る
    capital_budget_yen: int = 0
    # 信用口座に必要な委託保証金の最低額（円）。必要資金の計算に使う
    min_margin_deposit_yen: int = 300_000
    # 「人気」列が空欄の銘柄を自動判定するか（3月・9月の権利、または優待利回り1%以上なら「高」）
    auto_popularity: bool = True
    # 料率（methods）を最後に証券会社のサイトで確認した日。古くなったら警告する
    rates_checked_on: Optional[date] = date(2026, 9, 30)
    rates_stale_days: int = 90

    def enabled_methods(self) -> List[ShortMethod]:
        return [m for m in self.methods if m.enabled]

    def premium_ratio_for(self, popularity: str) -> float:
        return self.popularity_premium.get(popularity, self.inventory_premium_ratio)

    def rates_warning(self, today: date) -> Optional[str]:
        """料率の確認日が古いときの警告文。"""
        if self.rates_checked_on is None:
            return "設定の料率をいつ確認したか（rates_checked_on）が未入力です。証券会社の最新の料率を確認してください"
        days = (today - self.rates_checked_on).days
        if days > self.rates_stale_days:
            return (f"料率を確認した日（{self.rates_checked_on.isoformat()}）から{days}日たっています。"
                    "証券会社の最新の貸株料・手数料・返済期限を確認し、settings.json の methods と "
                    "rates_checked_on を更新してください")
        return None


def normalize_code(code: Any) -> str:
    """銘柄コードを正規化する（全角→半角、英字は大文字）。"""
    return unicodedata.normalize("NFKC", str(code)).strip().upper()


_NUM: Tuple[type, ...] = (int, float)
_SETTINGS_TYPES: Dict[str, Tuple[type, ...]] = {
    "valuation": (str,), "min_profit_yen": _NUM, "horizon_days": (int,),
    "inventory_premium_ratio": _NUM, "popularity_premium": (dict,),
    "max_lookback_business_days": (int,), "dividend_tax_recovered": (bool,),
    "benefit_tax_rate": _NUM, "margin_rate": _NUM, "seido_risk_basis": (str,),
    "seido_worst_multiplier": _NUM, "settlement_days": (int,), "include_long_term": (bool,),
    "capital_budget_yen": (int,), "min_margin_deposit_yen": (int,), "auto_popularity": (bool,),
    "rates_checked_on": (date, type(None)), "rates_stale_days": (int,),
}
_METHOD_TYPES: Dict[str, Tuple[type, ...]] = {
    "id": (str,), "broker": (str,), "label": (str,), "kind": (str,), "lending_rate": _NUM,
    "max_hold_days": (int, type(None)), "hold_basis": (str,), "day_count": (str,),
    "commission_buy": (int,), "commission_short": (int,),
    "dividend_adj_rate": (int, float, type(None)), "enabled": (bool,),
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


def _method_from_dict(d: Any) -> ShortMethod:
    if not isinstance(d, dict):
        raise ValueError("methods の各要素は { ... } の形で書いてください")
    d = _strip_comments(d)
    # 旧名（営業日数）からの読み替え
    if "max_hold_business_days" in d:
        d.setdefault("max_hold_days", d.pop("max_hold_business_days"))
        d.setdefault("hold_basis", BASIS_BUSINESS)
    missing = {"id", "broker", "label"} - set(d)
    if missing:
        raise ValueError(f"methods に必須の項目がありません: {sorted(missing)}")
    known = {f.name for f in fields(ShortMethod)}
    unknown = set(d) - known
    if unknown:
        raise ValueError(f"methods の未知の項目: {sorted(unknown)}")
    return ShortMethod(**d)


def settings_from_dict(data: Dict[str, Any]) -> Settings:
    data = _strip_comments(data)
    kwargs: Dict[str, Any] = {}
    if "methods" in data:
        methods = data.pop("methods")
        if not isinstance(methods, list):
            raise ValueError("methods は [ ... ] の形で書いてください")
        kwargs["methods"] = [_method_from_dict(m) for m in methods]
    if "extra_market_holidays" in data:
        kwargs["extra_market_holidays"] = [
            date.fromisoformat(str(s)) for s in data.pop("extra_market_holidays")
        ]
    if "exclude_codes" in data:
        kwargs["exclude_codes"] = [normalize_code(c) for c in data.pop("exclude_codes")]
    if "rates_checked_on" in data:
        v = data.pop("rates_checked_on")
        kwargs["rates_checked_on"] = date.fromisoformat(str(v)) if v else None
    known = {f.name for f in fields(Settings)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"設定ファイルの未知の項目: {sorted(unknown)}")
    kwargs.update(data)
    s = Settings(**kwargs)
    _check_types(s, _SETTINGS_TYPES, "")
    if s.valuation not in ("benefit", "resale"):
        raise ValueError("valuation は benefit か resale を指定してください")
    if s.seido_risk_basis not in ("worst", "expected"):
        raise ValueError("seido_risk_basis は worst か expected を指定してください")
    if s.horizon_days < 0 or s.max_lookback_business_days < 0 or s.settlement_days < 1:
        raise ValueError("horizon_days・max_lookback_business_days は0以上、settlement_days は1以上にしてください")
    if s.capital_budget_yen < 0 or s.min_margin_deposit_yen < 0:
        raise ValueError("capital_budget_yen・min_margin_deposit_yen は0以上にしてください")
    ids = [m.id for m in s.methods]
    if len(ids) != len(set(ids)):
        raise ValueError("methods の id が重複しています")
    for m in s.methods:
        _check_types(m, _METHOD_TYPES, f"methods[{m.id}].")
        if m.kind not in (KIND_IPPAN, KIND_SEIDO):
            raise ValueError(f"{m.id}: kind は ippan か seido を指定してください")
        if m.day_count not in ("both", "one"):
            raise ValueError(f"{m.id}: day_count は both か one を指定してください")
        if m.hold_basis not in (BASIS_BUSINESS, BASIS_CALENDAR):
            raise ValueError(f"{m.id}: hold_basis は business か calendar を指定してください")
        if m.max_hold_days is not None and m.max_hold_days < 1:
            raise ValueError(f"{m.id}: max_hold_days は1以上にしてください")
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
