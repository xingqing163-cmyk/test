"""クロス取引1回あたりのコスト計算（貸株料・逆日歩・手数料・配当の税ズレ）。"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import date
from typing import Optional

from .config import DIVIDEND_TAX_RATE, Settings, ShortMethod
from .jpx_calendar import MarketCalendar, RightsDates
from .watchlist import WatchItem

# 日証金「最高料率早見表」は株価帯ごとに 1株1日あたりの上限を定めており、
# 帯の上限株価のおよそ 0.2% になっている。ここではその近似を使う。
# 正確な値は日証金の早見表で確認し、ウォッチリストの「逆日歩最悪」列で上書きすること。
_MAX_RATE_BANDS = [
    100, 200, 300, 500, 700,
    1_000, 1_500, 2_000, 3_000, 5_000, 7_000,
    10_000, 15_000, 20_000, 30_000, 50_000, 70_000,
    100_000, 150_000, 200_000, 300_000, 500_000,
]
_MAX_RATE_RATIO = 0.002


def approx_max_gyakuhibu(price: float) -> float:
    """株価から「最高料率（1倍）」の概算（円/株/日）を返す。"""
    i = bisect.bisect_left(_MAX_RATE_BANDS, price)
    upper = _MAX_RATE_BANDS[i] if i < len(_MAX_RATE_BANDS) else price
    return round(upper * _MAX_RATE_RATIO, 2)


def lending_days(open_settle: date, close_settle: date, day_count: str) -> int:
    """貸株料の対象日数。受渡日ベースで、両端入れなら +1 日。"""
    days = (close_settle - open_settle).days
    if day_count == "both":
        return days + 1
    return max(days, 1)


def lending_fee(notional: float, annual_rate: float, days: int) -> int:
    """貸株料 = 約定代金 × 年率 × 日数 ÷ 365（円未満切り捨て）。"""
    return math.floor(notional * annual_rate * days / 365)


def dividend_cost(
    dividend_per_share: float, shares: int, adj_rate: float, tax_recovered: bool
) -> int:
    """配当まわりの実質コスト（マイナスなら得）。

    現物: 配当を源泉徴収(20.315%)後で受け取る。
    売建: 配当落調整金を支払う（一般信用は配当の100%、制度信用は84.685%）。
    特定口座(源泉徴収あり)＋株式数比例配分方式なら、支払った調整金は譲渡損として
    配当と自動で損益通算され、源泉税が（年間の損益に応じて）翌年初に還付される。
    """
    gross = dividend_per_share * shares
    paid = gross * adj_rate
    if tax_recovered:
        taxable = max(gross - paid, 0.0)
        net = gross - paid - taxable * DIVIDEND_TAX_RATE
    else:
        net = gross * (1 - DIVIDEND_TAX_RATE) - paid
    return round(-net)


@dataclass
class Quote:
    """ある売建方法・ある日にクロスした場合の損益見積もり。"""

    method: ShortMethod
    entry_date: date
    open_settle: date
    close_settle: date
    notional: int
    lending_days: int
    lending_fee: int
    gyakuhibu_days: int
    gyakuhibu_expected: int
    gyakuhibu_worst: int
    commissions: int
    dividend_cost: int
    value: int            # 優待の評価額
    benefit_tax: int      # 優待（雑所得）にかかる税の目安

    @property
    def cost_expected(self) -> int:
        return self.lending_fee + self.gyakuhibu_expected + self.commissions + self.dividend_cost

    @property
    def cost_worst(self) -> int:
        return self.lending_fee + self.gyakuhibu_worst + self.commissions + self.dividend_cost

    @property
    def net_expected(self) -> int:
        return self.value - self.benefit_tax - self.cost_expected

    @property
    def net_worst(self) -> int:
        return self.value - self.benefit_tax - self.cost_worst

    def decision_net(self, settings: Settings) -> int:
        """判定に使う利益。制度信用は設定に応じて最悪ケースで見る。"""
        if self.method.is_seido and settings.seido_risk_basis == "worst":
            return self.net_worst
        return self.net_expected


def quote(
    item: WatchItem,
    rights: RightsDates,
    method: ShortMethod,
    entry_date: date,
    cal: MarketCalendar,
    settings: Settings,
    price: Optional[float] = None,
) -> Quote:
    """entry_date に「現物買い＋信用売り」を約定させ、権利落ち日に現渡しした場合の見積もり。"""
    px = price if price is not None else item.price
    if px is None:
        raise ValueError(f"{item.code}: 株価が未設定です")
    if entry_date > rights.last_cum_date:
        raise ValueError("権利付最終日より後にクロスしても権利は取れません")

    notional = px * item.shares
    open_settle = cal.settlement_date(entry_date)
    close_settle = cal.settlement_date(rights.ex_date)  # 権利落ち日に現渡し
    l_days = lending_days(open_settle, close_settle, method.day_count)

    g_days = 0
    g_exp = g_worst = 0
    if method.is_seido:
        # 逆日歩は受渡日ベースの暦日数（週末・祝日をまたぐと日数が増える）
        g_days = (close_settle - open_settle).days
        est = item.gyakuhibu_est or 0.0
        worst_rate = item.gyakuhibu_worst
        if worst_rate is None:
            worst_rate = approx_max_gyakuhibu(px) * settings.seido_worst_multiplier
        g_exp = math.floor(est * item.shares * g_days)
        g_worst = max(g_exp, math.floor(worst_rate * item.shares * g_days))

    value = item.value(settings.valuation)
    return Quote(
        method=method,
        entry_date=entry_date,
        open_settle=open_settle,
        close_settle=close_settle,
        notional=round(notional),
        lending_days=l_days,
        lending_fee=lending_fee(notional, method.lending_rate, l_days),
        gyakuhibu_days=g_days,
        gyakuhibu_expected=g_exp,
        gyakuhibu_worst=g_worst,
        commissions=method.commission_buy + method.commission_short,
        dividend_cost=dividend_cost(
            item.dividend, item.shares, method.adj_rate, settings.dividend_tax_recovered
        ),
        value=value,
        benefit_tax=math.floor(value * settings.benefit_tax_rate),
    )
