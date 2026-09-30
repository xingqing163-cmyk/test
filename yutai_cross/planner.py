"""優待クロスの候補検出と、いつ・どの方法で建てるかの判定。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .config import BASIS_CALENDAR, Settings, ShortMethod
from .costs import Quote, quote
from .jpx_calendar import MarketCalendar, RightsDates, fmt_date, record_date_for
from .watchlist import WatchItem

COMMON_INVENTORY_WARNING = "一般信用の売り在庫は証券会社の画面で要確認（在庫がないと注文不可）"


@dataclass
class MethodPlan:
    method: ShortMethod
    window_start: date            # この方法で建てられる最初の日（短期なら「短期初日」）
    first_possible: date          # 今日以降で建てられる最初の営業日
    cheapest: Quote               # 権利付最終日にクロスした場合（最安）
    breakeven_date: Optional[date]  # 最低利益を確保できる最も早いエントリー日
    recommended: Optional[Quote]  # エントリー目安（ツールの試算上いちばん条件のよい日）
    reason: str = ""


@dataclass
class EventPlan:
    item: WatchItem
    rights: RightsDates
    method_plans: List[MethodPlan]
    best: Optional[MethodPlan]    # 試算上いちばん条件のよい方法（なければ None＝見送り）
    reference: Optional[MethodPlan]  # 見送り時にも表示用に使う一番ましな方法
    phase: str
    capital: int                  # 必要資金の目安（現物代金＋委託保証金）
    warnings: List[str] = field(default_factory=list)

    @property
    def shown(self) -> Optional[MethodPlan]:
        return self.best or self.reference

    @property
    def recommended(self) -> Optional[Quote]:
        return self.best.recommended if self.best else None

    @property
    def alternatives(self) -> List[MethodPlan]:
        """目安の方法の在庫がなかったときの代替（利益の大きい順）。"""
        alts = [mp for mp in self.method_plans if mp.recommended is not None and mp is not self.best]
        return sorted(alts, key=lambda mp: -mp.recommended.net_expected)

    @property
    def roi(self) -> Optional[float]:
        """必要資金に対する利益率（1回あたり）。"""
        q = self.recommended
        if q is None or self.capital <= 0:
            return None
        return q.net_expected / self.capital


@dataclass
class Action:
    day: date
    kind: str
    plan: EventPlan
    text: str


def short_window_start(cal: MarketCalendar, ex_date: date, method: ShortMethod) -> date:
    """一般信用（短期）で、返済期日が権利落ち日以降になる最初の約定日（短期初日）。"""
    n = method.max_hold_days
    if method.hold_basis == BASIS_CALENDAR:
        # 建日を1日目として n 日目（休日なら前営業日）が返済期日
        return cal.on_or_after(ex_date - timedelta(days=n - 1))
    return cal.add_business_days(ex_date, -n)


def short_due_date(cal: MarketCalendar, entry: date, method: ShortMethod) -> date:
    """一般信用（短期）の返済期日。"""
    n = method.max_hold_days
    if method.hold_basis == BASIS_CALENDAR:
        return cal.on_or_before(entry + timedelta(days=n - 1))
    return cal.add_business_days(entry, n)


def _broker_names(m: ShortMethod) -> Set[str]:
    """ウォッチリストの「一般信用」列で、この方法を指すと認める書き方。"""
    broker = m.broker.lower()
    names = {m.id.lower(), broker, broker + "証券"}
    # 「SBI短期」「楽天無期限」のように証券会社＋返済期限でも指定できる
    terms = ("短期",) if m.is_short_term else ("無期限", "長期")
    names |= {broker + t for t in terms} | {broker + "証券" + t for t in terms}
    return names


def methods_for(item: WatchItem, settings: Settings) -> List[ShortMethod]:
    """その銘柄で使える売建方法。"""
    result = []
    for m in settings.enabled_methods():
        if m.is_seido:
            if item.taishaku:
                result.append(m)
            continue
        if item.ippan_brokers is None:
            result.append(m)
            continue
        names = _broker_names(m)
        if any(b.lower() in names for b in item.ippan_brokers):
            result.append(m)
    return result


def unknown_brokers(item: WatchItem, settings: Settings) -> List[str]:
    """「一般信用」列のうち、設定のどの方法にも当たらない書き方。"""
    if not item.ippan_brokers:
        return []
    known: Set[str] = set()
    for m in settings.enabled_methods():
        if not m.is_seido:
            known |= _broker_names(m)
    return [b for b in item.ippan_brokers if b.lower() not in known]


def upcoming_rights(
    item: WatchItem, today: date, cal: MarketCalendar
) -> List[RightsDates]:
    """各権利月について、権利落ち日が今日以降の直近の権利日を返す。"""
    found = []
    for month in item.record_months:
        for year in (today.year, today.year + 1):
            rd = cal.rights_dates(record_date_for(year, month, item.record_day))
            if rd.ex_date >= today:
                found.append(rd)
                break
    return sorted(found, key=lambda r: r.last_cum_date)


def _business_days(cal: MarketCalendar, start: date, end: date) -> Iterable[date]:
    d = cal.on_or_after(start)
    while d <= end:
        yield d
        d = cal.next_business_day(d)


def plan_method(
    item: WatchItem,
    rights: RightsDates,
    method: ShortMethod,
    today: date,
    cal: MarketCalendar,
    settings: Settings,
    price: float,
) -> MethodPlan:
    last = rights.last_cum_date
    if method.is_seido:
        window_start = last  # 制度信用は早く建てても得がないので最終日のみ
    elif method.is_short_term:
        # 返済期限内に「権利落ち日の現渡し」が収まる最初の日
        window_start = short_window_start(cal, rights.ex_date, method)
    else:
        window_start = cal.add_business_days(last, -settings.max_lookback_business_days)
    first_possible = max(window_start, cal.on_or_after(today))

    def q(d: date) -> Quote:
        return quote(item, rights, method, d, cal, settings, price=price)

    cheapest = q(last)
    cheap_net = cheapest.decision_net(settings)

    breakeven = None
    for d in _business_days(cal, window_start, last):
        if q(d).decision_net(settings) >= settings.min_profit_yen:
            breakeven = d
            break

    if first_possible > last:
        return MethodPlan(method, window_start, first_possible, cheapest, breakeven, None,
                          "権利付最終日を過ぎています")
    if cheap_net < settings.min_profit_yen:
        return MethodPlan(method, window_start, first_possible, cheapest, breakeven, None,
                          f"利益が最低ライン({settings.min_profit_yen:,}円)未満")

    ratio = settings.premium_ratio_for(item.popularity)
    target = max(settings.min_profit_yen, math.ceil(cheap_net * (1 - ratio)))
    recommended = cheapest
    for d in _business_days(cal, first_possible, last):
        cand = q(d)
        if cand.decision_net(settings) >= target:
            recommended = cand
            break
    if recommended.entry_date == last:
        reason = "コスト最小の権利付最終日"
    else:
        reason = f"在庫確保のため先回り（利益の{ratio:.0%}までを貸株料に充当）"
    return MethodPlan(method, window_start, first_possible, cheapest, breakeven, recommended, reason)


def _phase(today: date, item: WatchItem, rights: RightsDates, rec: Optional[Quote],
           cal: MarketCalendar) -> str:
    if today > rights.ex_date:
        return "終了"
    if today == rights.ex_date:
        return "本日現渡し"
    if today > rights.last_cum_date:
        return f"現渡し待ち（{fmt_date(rights.ex_date)}）"
    if item.crossed:
        return f"クロス済み（現渡しは{rights.ex_date.month}/{rights.ex_date.day}）"
    if rec is None:
        return "見送り"
    if today == rights.last_cum_date:
        return "本日が権利付最終日"
    if today >= rec.entry_date:
        return "エントリー期間中"
    n = cal.business_days_between(today, rec.entry_date)
    return f"待機（あと{n}営業日）"


def plan_event(
    item: WatchItem,
    rights: RightsDates,
    today: date,
    cal: MarketCalendar,
    settings: Settings,
    price: float,
) -> EventPlan:
    warnings: List[str] = list(item.notes)
    methods = methods_for(item, settings)
    mplans = [plan_method(item, rights, m, today, cal, settings, price) for m in methods]

    if item.long_term and not settings.include_long_term:
        for mp in mplans:
            mp.recommended = None
            mp.reason = "長期保有条件があるため見送り（include_long_term で変更可）"
    usable = [mp for mp in mplans if mp.recommended is not None]

    def score(mp: MethodPlan, q: Quote) -> Tuple[int, int, int]:
        return (q.decision_net(settings), 0 if mp.method.is_seido else 1, q.net_worst)

    best = max(usable, key=lambda mp: score(mp, mp.recommended)) if usable else None
    reference = (
        max(mplans, key=lambda mp: score(mp, mp.cheapest)) if mplans and best is None else None
    )

    notional = price * item.shares
    capital = math.ceil(notional + notional * settings.margin_rate)

    unknown = unknown_brokers(item, settings)
    if unknown:
        warnings.append(
            f"「一般信用」列の {'、'.join(unknown)} を読めません"
            "（書き方の例: SBI短期;楽天短期 / SBI無期限 / 楽天。空欄なら全社・全方法を候補）"
        )
    if not methods:
        warnings.append("売建できる方法がありません（一般信用の取扱いなし・貸借銘柄でもない）")
    if item.long_term:
        warnings.append(f"長期保有条件あり（{item.long_term}）→ 1回のクロスでは優待が出ない可能性")
    if item.record_day != "末":
        warnings.append(f"基準日が{str(item.record_day).rstrip('日')}日（月末ではない）")

    shown = best or reference
    if shown is not None:
        q = shown.recommended or shown.cheapest
        if shown.method.is_seido:
            basis = ("ウォッチリストの「逆日歩最悪」" if item.gyakuhibu_worst is not None
                     else f"最高料率×{settings.seido_worst_multiplier:g}倍")
            warnings.append(
                f"制度信用: 逆日歩を{basis}で見積もると {q.gyakuhibu_worst:,}円（{q.gyakuhibu_days}日分）。"
                "臨時措置で10倍になるとさらに増える（上限ではない）。一般信用の在庫が取れたらそちらを優先"
            )
            if settings.seido_risk_basis == "expected" and item.gyakuhibu_est is None:
                warnings.append("「逆日歩想定」が未入力なので、想定値も上の見積もりで計算しています")
            if q.gyakuhibu_days >= 3:
                warnings.append(f"権利付最終日の後に休日があり逆日歩が{q.gyakuhibu_days}日分かかる")
        else:
            warnings.append(COMMON_INVENTORY_WARNING)
        if q.dividend_cost > 0 and not settings.dividend_tax_recovered:
            warnings.append(f"配当の源泉税ズレ {q.dividend_cost:,}円（特定口座の損益通算で戻せる設定なら0）")

    phase = _phase(today, item, rights, best.recommended if best else None, cal)
    return EventPlan(item, rights, mplans, best, reference, phase, capital, warnings)


@dataclass
class PlanResult:
    today: date
    plans: List[EventPlan]
    skipped: List[str]            # 株価なし・除外などで計画できなかった銘柄

    @property
    def recommended(self) -> List[EventPlan]:
        return [p for p in self.plans if p.best is not None]

    @property
    def passed(self) -> List[EventPlan]:
        """権利付最終日前だが、利益不足などで見送りの銘柄。"""
        return [p for p in self.plans if p.best is None and self.today <= p.rights.last_cum_date]

    @property
    def settling(self) -> List[EventPlan]:
        """権利付最終日を過ぎ、クロス済みなら現渡しを待つ銘柄。"""
        return [p for p in self.plans if self.today > p.rights.last_cum_date]


def build_plan(
    items: List[WatchItem],
    today: date,
    settings: Settings,
    prices: Optional[Dict[str, float]] = None,
    cal: Optional[MarketCalendar] = None,
) -> PlanResult:
    cal = cal or MarketCalendar(settings.extra_market_holidays, settings.settlement_days)
    prices = prices or {}
    excluded = set(settings.exclude_codes)
    plans: List[EventPlan] = []
    skipped: List[str] = []
    horizon_end = today + timedelta(days=settings.horizon_days)

    for item in items:
        if not item.enabled:
            continue
        if item.code in excluded:
            skipped.append(f"{item.code} {item.name}: 除外リスト（exclude_codes）に含まれるためスキップ")
            continue
        price = prices.get(item.code, item.price)
        if price is None or price <= 0:
            skipped.append(f"{item.code} {item.name}: 株価が未設定のためスキップ")
            continue
        for rights in upcoming_rights(item, today, cal):
            if rights.last_cum_date > horizon_end:
                continue
            plans.append(plan_event(item, rights, today, cal, settings, price))

    def sort_key(p: EventPlan):
        q = p.recommended
        return (0 if q else 1, -(q.net_expected if q else -10**9), p.rights.last_cum_date)

    plans.sort(key=sort_key)
    return PlanResult(today, plans, skipped)


def peak_capital(plans: List[EventPlan]) -> Tuple[int, Optional[date]]:
    """目安どおりに建てた場合に同時に必要となる資金のピーク（目安）。"""
    events: Dict[date, int] = {}
    for p in plans:
        q = p.recommended
        if q is None:
            continue
        events[q.entry_date] = events.get(q.entry_date, 0) + p.capital
        release = q.close_settle + timedelta(days=1)
        events[release] = events.get(release, 0) - p.capital
    running, peak, peak_day = 0, 0, None
    for d in sorted(events):
        running += events[d]
        if running > peak:
            peak, peak_day = running, d
    return peak, peak_day


def order_text(label: str, shares: int) -> str:
    """講座どおりの発注順（売り在庫を先に押さえてから現物を買う）。"""
    return f"①{label} 売り(寄成) {shares:,}株 → ②現物買い(寄成・預り区分「特定」) {shares:,}株"


def actions_on(day: date, result: PlanResult) -> List[Action]:
    """指定日にやるべきこと（その日の寄付〜大引けで約定させる注文・現渡し）。"""
    actions: List[Action] = []
    for p in result.plans:
        name = f"{p.item.code} {p.item.name}"
        rd = p.rights
        if day == rd.ex_date and p.method_plans:
            actions.append(Action(day, "現渡し", p,
                                  f"{name}: 権利落ち日。クロスした建玉があれば「現渡し（品渡し）」で決済"
                                  "（返済買いではない）"))
        q = p.recommended
        if q is None or day > rd.last_cum_date or p.item.crossed:
            continue
        label = p.best.method.label
        orders = order_text(label, p.item.shares)
        alts = [mp.method.label for mp in p.alternatives if mp.first_possible <= day]
        alt_text = f"（在庫がなければ {' → '.join(alts)}）" if alts else ""
        if day == rd.last_cum_date:
            actions.append(Action(day, "最終日", p,
                                  f"{name}: 権利付最終日。まだクロスしていなければ寄付前に「{orders}」"
                                  f"の順で発注{alt_text}。寄付に間に合わなければ、両方とも引成・同じ株数で"
                                  "続けて発注（取引時間中に成行や指値で売り買いしない）"))
        elif day >= q.entry_date:
            head = "エントリー目安日" if day == q.entry_date else "エントリー期間中"
            actions.append(Action(day, "エントリー", p,
                                  f"{name}: {head}（まだクロスしていなければ）。在庫を確認して「{orders}」"
                                  f"の順で発注{alt_text}。見込み利益 {q.net_expected:,}円・最終日 "
                                  f"{fmt_date(rd.last_cum_date)}。建てたらウォッチリストの「クロス済」に○"))
        for mp in p.method_plans:
            if not mp.method.is_short_term or day != mp.window_start:
                continue
            if mp is p.best and q.entry_date == day:
                continue  # エントリーの通知と重なるので出さない
            actions.append(Action(day, "短期初日", p,
                                  f"{name}: {mp.method.label} で建てられる初日。人気銘柄は前夜〜寄付前に"
                                  f"在庫がなくなりやすい（参考。目安は {fmt_date(q.entry_date)} の {label}）"))
    order = {"現渡し": 0, "最終日": 1, "エントリー": 2, "短期初日": 3}
    actions.sort(key=lambda a: (order.get(a.kind, 9), a.plan.item.code))
    return actions


def next_action_day(result: PlanResult, cal: MarketCalendar, after: date,
                    limit_days: int = 120) -> Optional[Tuple[date, List[Action]]]:
    """after より後で、最初にやることがある営業日。"""
    d = cal.next_business_day(after)
    end = after + timedelta(days=limit_days)
    while d <= end:
        acts = actions_on(d, result)
        if acts:
            return d, acts
        d = cal.next_business_day(d)
    return None
