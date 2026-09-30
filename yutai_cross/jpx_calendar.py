"""東証（JPX）の営業日カレンダーと、権利付最終日・権利落ち日の計算。

外部ライブラリに頼らず、祝日法のルールから日本の祝日を計算する。
対応年は 2020〜2099 年（春分・秋分の近似式の有効範囲内）。
法改正や臨時休場があった場合は MarketCalendar(extra_holidays=...) で補える。
"""

from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Dict, Iterable, Optional

# 株式の受渡しは約定日から2営業日後（2019年7月16日約定分から T+2）
DEFAULT_SETTLEMENT_DAYS = 2

MIN_YEAR = 2020
MAX_YEAR = 2099

MONDAY, SUNDAY = 0, 6


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """year年month月の第n weekday（月曜=0）を返す。"""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _vernal_equinox(year: int) -> date:
    """春分日（1980〜2099年で有効な近似式）。"""
    day = int(20.8431 + 0.242194 * (year - 1980) - (year - 1980) // 4)
    return date(year, 3, day)


def _autumnal_equinox(year: int) -> date:
    """秋分日（1980〜2099年で有効な近似式）。"""
    day = int(23.2488 + 0.242194 * (year - 1980) - (year - 1980) // 4)
    return date(year, 9, day)


def _base_holidays(year: int) -> Dict[date, str]:
    """振替休日・国民の休日を含まない「国民の祝日」。"""
    h: Dict[date, str] = {
        date(year, 1, 1): "元日",
        _nth_weekday(year, 1, MONDAY, 2): "成人の日",
        date(year, 2, 11): "建国記念の日",
        date(year, 2, 23): "天皇誕生日",
        _vernal_equinox(year): "春分の日",
        date(year, 4, 29): "昭和の日",
        date(year, 5, 3): "憲法記念日",
        date(year, 5, 4): "みどりの日",
        date(year, 5, 5): "こどもの日",
        _autumnal_equinox(year): "秋分の日",
        _nth_weekday(year, 9, MONDAY, 3): "敬老の日",
        date(year, 11, 3): "文化の日",
        date(year, 11, 23): "勤労感謝の日",
    }
    # 東京オリンピック・パラリンピック特措法による移動（2020年・2021年）
    if year == 2020:
        h[date(2020, 7, 23)] = "海の日"
        h[date(2020, 7, 24)] = "スポーツの日"
        h[date(2020, 8, 10)] = "山の日"
    elif year == 2021:
        h[date(2021, 7, 22)] = "海の日"
        h[date(2021, 7, 23)] = "スポーツの日"
        h[date(2021, 8, 8)] = "山の日"
    else:
        h[_nth_weekday(year, 7, MONDAY, 3)] = "海の日"
        h[date(year, 8, 11)] = "山の日"
        h[_nth_weekday(year, 10, MONDAY, 2)] = "スポーツの日"
    return h


@lru_cache(maxsize=None)
def japanese_holidays(year: int) -> Dict[date, str]:
    """year年の祝日（国民の祝日＋国民の休日＋振替休日）を {日付: 名称} で返す。"""
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise ValueError(f"{year}年は対象外です（{MIN_YEAR}〜{MAX_YEAR}年に対応）")
    base = _base_holidays(year)
    result = dict(base)

    # 国民の休日: 前日と翌日が国民の祝日である平日
    for d in sorted(base):
        between = d + timedelta(days=1)
        if (
            between not in base
            and between + timedelta(days=1) in base
            and between.weekday() != SUNDAY
        ):
            result[between] = "国民の休日"

    # 振替休日: 祝日が日曜の場合、その後の最初の「祝日でない日」
    for d in sorted(base):
        if d.weekday() == SUNDAY:
            sub = d + timedelta(days=1)
            while sub in result:
                sub += timedelta(days=1)
            result[sub] = "振替休日"
    return result


def is_market_closed_by_rule(d: date) -> bool:
    """土日・祝日・年末年始（12/31〜1/3）なら True。"""
    if d.weekday() >= 5:
        return True
    if (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3):
        return True
    return d in japanese_holidays(d.year)


@dataclass(frozen=True)
class RightsDates:
    """ある権利確定日（基準日）に対応する重要日付。"""

    record_date: date            # 権利確定日（会社が決めた基準日。休日のこともある）
    effective_record_date: date  # 基準日以前の最終営業日（この日までに受渡しが必要）
    last_cum_date: date          # 権利付最終日（この日の取引終了時点で保有していれば権利獲得）
    ex_date: date                # 権利落ち日（現渡しで手仕舞う日）


class MarketCalendar:
    """東証の営業日カレンダー。"""

    def __init__(
        self,
        extra_holidays: Iterable[date] = (),
        settlement_days: int = DEFAULT_SETTLEMENT_DAYS,
    ) -> None:
        self.extra_holidays = frozenset(extra_holidays)
        self.settlement_days = settlement_days

    def is_business_day(self, d: date) -> bool:
        return not (is_market_closed_by_rule(d) or d in self.extra_holidays)

    def holiday_name(self, d: date) -> Optional[str]:
        if d in self.extra_holidays:
            return "臨時休場"
        if (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3 and d.day != 1):
            return "年末年始休場"
        return japanese_holidays(d.year).get(d)

    def add_business_days(self, d: date, n: int) -> date:
        """d から n 営業日後（n<0 なら前）の営業日。d 自身が休日でも数え方は同じ。"""
        step = 1 if n >= 0 else -1
        remaining = abs(n)
        cur = d
        while remaining:
            cur += timedelta(days=step)
            if self.is_business_day(cur):
                remaining -= 1
        return cur

    def on_or_before(self, d: date) -> date:
        while not self.is_business_day(d):
            d -= timedelta(days=1)
        return d

    def on_or_after(self, d: date) -> date:
        while not self.is_business_day(d):
            d += timedelta(days=1)
        return d

    def next_business_day(self, d: date) -> date:
        return self.add_business_days(d, 1)

    def business_days_between(self, start: date, end: date) -> int:
        """start の翌日から end まで（end を含む）の営業日数。end < start なら負。"""
        if end < start:
            return -self.business_days_between(end, start)
        count = 0
        cur = start
        while cur < end:
            cur += timedelta(days=1)
            if self.is_business_day(cur):
                count += 1
        return count

    def settlement_date(self, trade_date: date) -> date:
        """約定日 → 受渡日。"""
        return self.add_business_days(trade_date, self.settlement_days)

    def rights_dates(self, record_date: date) -> RightsDates:
        effective = self.on_or_before(record_date)
        last_cum = self.add_business_days(effective, -self.settlement_days)
        return RightsDates(
            record_date=record_date,
            effective_record_date=effective,
            last_cum_date=last_cum,
            ex_date=self.next_business_day(last_cum),
        )


def record_date_for(year: int, month: int, day_spec: str | int) -> date:
    """基準日の指定（"末" / 20 など）から日付を作る。月の日数を超える日は月末に丸める。"""
    last_day = _calendar.monthrange(year, month)[1]
    if isinstance(day_spec, str):
        spec = day_spec.strip()
        if spec in ("", "末", "末日", "end", "END", "last"):
            return date(year, month, last_day)
        spec = spec.rstrip("日")
        day = int(spec)
    else:
        day = int(day_spec)
    if day < 1:
        raise ValueError(f"基準日の日付が不正です: {day_spec!r}")
    return date(year, month, min(day, last_day))


WEEKDAY_JA = "月火水木金土日"


def fmt_date(d: Optional[date]) -> str:
    """2026-12-28(月) 形式。"""
    if d is None:
        return "-"
    return f"{d.isoformat()}({WEEKDAY_JA[d.weekday()]})"
