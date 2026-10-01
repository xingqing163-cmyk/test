"""計画結果の出力（端末表示・Markdown・CSV・iCalendar）。"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import date, datetime, timezone
from typing import List, Optional, Sequence

from .config import Settings
from .costs import Quote
from .jpx_calendar import WEEKDAY_JA, MarketCalendar, fmt_date
from .planner import (
    COMMON_INVENTORY_WARNING,
    Action,
    EventPlan,
    MethodPlan,
    PlanResult,
    actions_on,
    peak_capital,
    position_alerts,
    position_due_date,
    short_due_date,
)

DISCLAIMER = ("※ 入力値と設定にもとづく試算です。特定の銘柄の売買を勧めるものではありません。"
              "料率・在庫・優待内容は変わるので、発注前に証券会社と会社IRで最新情報を確認してください。")


def short_date(d: Optional[date]) -> str:
    if d is None:
        return "-"
    return f"{d.month:>2}/{d.day:02}({WEEKDAY_JA[d.weekday()]})"


def yen(v: Optional[int]) -> str:
    return "-" if v is None else f"{v:,}"


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _fit(s: str, width: int, right: bool = False) -> str:
    """表示幅 width に切り詰め／空白埋めする（全角は幅2）。"""
    out, w = "", 0
    for c in s:
        cw = _width(c)
        if w + cw > width:
            break
        out += c
        w += cw
    pad = " " * (width - w)
    return pad + out if right else out + pad


def _table(headers: Sequence[str], rows: List[Sequence[str]], widths: Sequence[int],
           right_cols: Sequence[int] = ()) -> str:
    lines = [" ".join(_fit(h, w, i in right_cols) for i, (h, w) in enumerate(zip(headers, widths)))]
    lines.append(" ".join("-" * w for w in widths))
    for r in rows:
        lines.append(" ".join(_fit(c, w, i in right_cols) for i, (c, w) in enumerate(zip(r, widths))))
    return "\n".join(lines)


def short_method(m) -> str:
    """表の中で使う短い方法名（例: SBI 短期、楽天 無期限、制度信用）。"""
    if m.is_seido:
        return "制度信用"
    return f"{m.broker} {'短期' if m.is_short_term else '無期限'}"


def _short_phase(phase: str) -> str:
    """表の「状態」列用の短い表記。"""
    if phase.startswith("待機（"):
        return phase[len("待機（"):].rstrip("）")
    if phase.startswith("クロス済み"):
        return "クロス済"
    return phase.replace("本日が権利付最終日", "本日が最終日")


def _valuation_label(settings: Settings) -> str:
    return "換金価値" if settings.valuation == "resale" else "優待価値（自分で使う場合）"


def cost_breakdown(q: Quote) -> str:
    parts = [f"貸株料 {q.lending_fee:,}円（{q.lending_days}日分）"]
    if q.method.is_seido:
        parts.append(f"逆日歩 想定{q.gyakuhibu_expected:,}円／上限側の見積もり{q.gyakuhibu_worst:,}円"
                     f"（{q.gyakuhibu_days}日分）")
    parts.append(f"手数料 {q.commissions:,}円")
    if q.dividend_cost:
        sign = "損" if q.dividend_cost > 0 else "得"
        parts.append(f"配当まわり {abs(q.dividend_cost):,}円{sign}")
    if q.benefit_tax:
        parts.append(f"優待の税(目安) {q.benefit_tax:,}円")
    return " / ".join(parts)


def _actions_text(title: str, actions: List[Action]) -> List[str]:
    lines = [title]
    if not actions:
        lines.append("  （なし）")
    for a in actions:
        lines.append(f"  - [{a.kind}] {a.text}")
    return lines


def _attention_lines(result: PlanResult, settings: Settings) -> List[str]:
    """料率の確認日・建玉の要対応など、最初に見てほしいこと。"""
    lines = []
    rates = settings.rates_warning(result.today)
    alerts = position_alerts(result)
    if rates or alerts:
        lines.append("■ 先に確認してください")
        if rates:
            lines.append(f"  - {rates}")
        lines += [f"  - {a}" for a in alerts]
        lines.append("")
    return lines


def _passed_reason(p: EventPlan, settings: Settings) -> str:
    if p.excluded_reason:
        return p.excluded_reason
    mp = p.shown
    if mp is None:
        return "売建できる方法なし"
    return (f"{mp.reason}（参考: {mp.method.label}で最終日にクロスした場合の利益 "
            f"{mp.cheapest.decision_net(settings):,}円）")


def render_console(result: PlanResult, settings: Settings, cal: MarketCalendar) -> str:
    today = result.today
    out: List[str] = []
    bar = "=" * 80
    out += [
        bar,
        f" 優待クロス 自動検出レポート   基準日: {fmt_date(today)}",
        f" 評価: {_valuation_label(settings)} / 最低利益 {settings.min_profit_yen:,}円 / "
        f"権利付最終日が{settings.horizon_days}日先までの銘柄",
        bar,
        "",
    ]
    out += _attention_lines(result, settings)
    if cal.is_business_day(today):
        out += _actions_text(f"■ 今日 {fmt_date(today)} にやること", actions_on(today, result))
        out.append("")
    nxt = cal.next_business_day(today)
    out += _actions_text(
        f"■ 次の営業日 {fmt_date(nxt)} にやること（寄付で約定させる注文は今夜〜当日8:59までに）",
        actions_on(nxt, result),
    )
    out.append("")

    rec = result.recommended
    out.append("■ 条件を満たす候補（見込み利益の大きい順・試算）")
    if rec:
        headers = ["コード", "銘柄", "クロス日", "どこで", "付最終日", "利益", "状態"]
        widths = [6, 20, 10, 10, 10, 7, 18]
        rows = []
        for p in rec:
            mp = p.best
            q = mp.recommended
            rows.append([
                p.item.code, p.item.name, short_date(q.entry_date), short_method(mp.method),
                short_date(p.rights.last_cum_date), yen(q.net_expected), _short_phase(p.phase),
            ])
        out.append(_table(headers, rows, widths, right_cols=(5,)))
        out.append("  ※クロス日＝エントリーの目安日。利益＝優待価値−貸株料など（円）。"
                   "方法別の比較・建て可能日・損益分岐日は plan -o のレポートに")
    else:
        out.append("  （条件を満たす候補はありません）")
    out.append("")

    if result.passed:
        out.append("■ 見送り（利益不足・売建不可・資金枠など）")
        for p in result.passed:
            out.append(f"  - {p.item.code} {p.item.name} {p.rights.record_date.month}月権利"
                       f"（付最終日 {short_date(p.rights.last_cum_date)}）: {_passed_reason(p, settings)}")
        out.append("")

    if result.settling:
        out.append("■ 権利付最終日を過ぎた銘柄（クロス済みなら権利落ち日以降に現渡し）")
        for p in result.settling:
            out.append(f"  - {p.item.code} {p.item.name}: 現渡し {fmt_date(p.rights.ex_date)}〜（{p.phase}）")
        out.append("")

    warn_lines = []
    if any(COMMON_INVENTORY_WARNING in p.warnings for p in result.recommended):
        warn_lines.append(f"  - 全銘柄共通: {COMMON_INVENTORY_WARNING}")
    for p in result.recommended + result.passed:
        for w in p.warnings:
            # 見送りの銘柄は、入力の読み違いなど直すべき注意だけ出す
            if w == COMMON_INVENTORY_WARNING or (p.recommended is None and "読め" not in w):
                continue
            warn_lines.append(f"  - [{p.item.code} {p.rights.record_date.month}月] {w}")
    if warn_lines:
        out.append("■ 注意事項")
        out += warn_lines
        out.append("")
    if result.skipped:
        out.append("■ 計画できなかった銘柄")
        out += [f"  - {s}" for s in result.skipped]
        out.append("")

    peak, peak_day = peak_capital(result)
    if peak:
        budget = settings.capital_budget_yen
        budget_text = f"（資金枠 {budget:,}円）" if budget else "（資金枠なし。--budget で上限を指定できます）"
        out.append(f"■ 目安どおりに建てた場合の必要資金ピーク: {peak:,}円（{fmt_date(peak_day)}ごろ）{budget_text}")
        out.append(f"   ※現物代金＋委託保証金（建玉の{settings.margin_rate:.0%}、最低{settings.min_margin_deposit_yen:,}円）。"
                   "保有中の建玉の記録も含む")
    out.append(DISCLAIMER)
    return "\n".join(out)


def _method_table_md(p: EventPlan, settings: Settings) -> List[str]:
    lines = [
        "| 方法 | 建て可能開始 | 損益分岐日 | 最終日クロス利益 | 目安日 | 目安日の利益 | 上限側で見た利益 | 備考 |",
        "|---|---|---|---:|---|---:|---:|---|",
    ]
    for mp in p.method_plans:
        r = mp.recommended
        lines.append(
            f"| {mp.method.label} | {fmt_date(mp.window_start)} | {fmt_date(mp.breakeven_date)} | "
            f"{mp.cheapest.decision_net(settings):,}円 | {fmt_date(r.entry_date) if r else '-'} | "
            f"{f'{r.net_expected:,}円' if r else '-'} | {f'{r.net_worst:,}円' if r else '-'} | "
            f"{mp.reason} |"
        )
    return lines


_TSE_CODE = re.compile(r"\d{3}[0-9A-Z]")


def _links(code: str) -> str:
    """銘柄の参考リンク（東証の銘柄コードの形のときだけ）。"""
    if not _TSE_CODE.fullmatch(code):
        return ""
    return (f"[株探](https://kabutan.jp/stock/?code={code}) ／ "
            f"[Yahoo!ファイナンス](https://finance.yahoo.co.jp/quote/{code}.T)")


def _plan_md(i: int, p: EventPlan, settings: Settings, cal: MarketCalendar) -> List[str]:
    it, rd = p.item, p.rights
    title = f"## {i}. {it.code} {it.name} — {rd.record_date.month}月権利"
    lines = [title, ""]
    value_note = f"{it.benefit_value:,}円"
    if it.resale_value is not None:
        value_note += f"（換金なら {it.resale_value:,}円）"
    lines += [
        f"- 優待: {it.benefit or '（内容未入力）'} / 優待価値 {value_note} / 必要 {it.shares:,}株",
        f"- 権利確定日: {fmt_date(rd.record_date)} ／ **権利付最終日: {fmt_date(rd.last_cum_date)}** ／ "
        f"権利落ち日(現渡し日): {fmt_date(rd.ex_date)}",
        f"- 状態: {p.phase}" + (f" ／ 人気: {p.popularity}（自動判定）" if p.popularity_auto else ""),
    ]
    links = _links(it.code)
    if links:
        lines.append(f"- 参考リンク: {links}（優待内容・権利日は必ず会社のIRで確認）")
    if p.position is not None:
        lines.append(f"- 建玉の記録: #{p.position.id} {fmt_date(p.position.entry_date)}建て・"
                     f"{p.position.shares:,}株・{p.position.method_id}")
    mp = p.best
    if mp is not None:
        q = mp.recommended
        due = short_due_date(cal, q.entry_date, mp.method) if mp.method.is_short_term else None
        due_text = f"（返済期日 {fmt_date(due)}。過ぎると強制決済）" if due else ""
        lines += [
            f"- **試算上の目安: {mp.method.label} で {fmt_date(q.entry_date)} にクロス → "
            f"{fmt_date(rd.ex_date)} に現渡し**（{mp.reason}）",
            f"- 見込み利益: **{q.net_expected:,}円**（優待価値 {q.value:,}円 − コスト {q.cost_expected:,}円）",
            f"- コスト内訳: {cost_breakdown(q)}",
            f"- 必要資金目安: {p.capital:,}円（1回の利回り {p.roi:.2%}）",
            "",
            "**手順**",
            "",
            f"1. {fmt_date(cal.add_business_days(q.entry_date, -1))} の夜〜{fmt_date(q.entry_date)} 8:59: "
            f"{mp.method.label} の売り在庫があるか確認",
            f"2. 先に「①信用新規売り {it.shares:,}株・成行・寄付(寄成)・{mp.method.term_label}」、"
            f"通ったらすぐ「②現物買い {it.shares:,}株・成行・寄付(寄成)・預り区分 特定」を発注",
            "3. 9:00の寄付で約定したら、現物と売建玉が同じ株数・同じ値段か確認"
            "（片方だけなら講座第4回 STEP5 の対処）。建てたらウォッチリストの「クロス済」に○",
            f"4. {fmt_date(rd.ex_date)}（権利落ち日）に、売建玉を「現渡し（品渡し）」で決済{due_text}。"
            "権利付最終日に現渡しすると権利がなくなるので注意",
        ]
    else:
        lines.append(f"- 判定: **見送り**（{_passed_reason(p, settings)}）")
    lines += ["", *_method_table_md(p, settings)]
    if p.warnings:
        lines += ["", "**注意**", ""] + [f"- {w}" for w in p.warnings]
    lines.append("")
    return lines


def render_markdown(result: PlanResult, settings: Settings, cal: MarketCalendar) -> str:
    today = result.today
    lines = [
        f"# 優待クロス計画レポート（{fmt_date(today)} 時点）",
        "",
        f"- 評価方法: {_valuation_label(settings)} / 最低利益: {settings.min_profit_yen:,}円 / "
        f"対象: 権利付最終日が{settings.horizon_days}日先まで",
        f"- 配当の源泉税: {'特定口座の損益通算で戻る前提' if settings.dividend_tax_recovered else '戻らない前提'}",
        "- 制度信用の判定: "
        + (f"逆日歩を最高料率×{settings.seido_worst_multiplier:g}倍で見積もって評価"
           if settings.seido_risk_basis == "worst" else "逆日歩の想定値で評価"),
        "",
        f"> {DISCLAIMER}",
        "",
    ]
    attention = _attention_lines(result, settings)
    if attention:
        lines += ["## 先に確認してください", ""] + [l.replace("  - ", "- ", 1) for l in attention[1:-1]] + [""]
    nxt = cal.next_business_day(today)
    for title, day in (("今日", today), ("次の営業日", nxt)):
        if day == today and not cal.is_business_day(today):
            continue
        acts = actions_on(day, result)
        lines.append(f"### {title} {fmt_date(day)} にやること")
        lines.append("")
        lines += [f"- [{a.kind}] {a.text}" for a in acts] or ["- （なし）"]
        lines.append("")

    rec = result.recommended
    lines += ["## 条件を満たす候補（試算）", ""]
    if rec:
        lines += [
            "| No | コード | 銘柄 | 権利付最終日 | 方法 | エントリー目安日 | 見込み利益 | 利回り | 状態 |",
            "|---:|---|---|---|---|---|---:|---:|---|",
        ]
        for i, p in enumerate(rec, 1):
            q = p.recommended
            lines.append(
                f"| {i} | {p.item.code} | {p.item.name} | {fmt_date(p.rights.last_cum_date)} | "
                f"{p.best.method.label} | {fmt_date(q.entry_date)} | {q.net_expected:,}円 | "
                f"{p.roi:.2%} | {p.phase} |"
            )
    else:
        lines.append("条件を満たす候補はありません。")
    lines.append("")
    peak, peak_day = peak_capital(result)
    if peak:
        lines += [f"目安どおりに建てた場合の必要資金ピーク: **{peak:,}円**（{fmt_date(peak_day)}ごろ。"
                  f"最低保証金{settings.min_margin_deposit_yen:,}円を含む）", ""]

    for i, p in enumerate(rec, 1):
        lines += _plan_md(i, p, settings, cal)
    if result.passed:
        lines += ["# 見送り銘柄", ""]
        for i, p in enumerate(result.passed, 1):
            lines += _plan_md(i, p, settings, cal)
    if result.settling:
        lines += ["# 権利付最終日を過ぎた銘柄（クロス済みなら現渡し）", ""]
        lines += [f"- {p.item.code} {p.item.name}: 現渡し {fmt_date(p.rights.ex_date)}〜（{p.phase}）"
                  for p in result.settling] + [""]
    if result.skipped:
        lines += ["# 計画できなかった銘柄", ""] + [f"- {s}" for s in result.skipped] + [""]
    return "\n".join(lines)


CSV_HEADERS = [
    "判定", "状態", "コード", "銘柄", "権利月", "権利確定日", "権利付最終日", "権利落ち日",
    "方法", "エントリー目安日", "建て可能開始日", "損益分岐日", "優待価値", "貸株料",
    "逆日歩(想定)", "逆日歩(上限側)", "手数料", "配当まわり", "コスト合計", "見込み利益",
    "上限側で見た利益", "必要資金", "利回り", "注意",
]


def render_csv(result: PlanResult, settings: Settings) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADERS)
    for p in result.plans:
        mp: Optional[MethodPlan] = p.shown
        q = (mp.recommended or mp.cheapest) if mp else None
        if p.best:
            verdict = "候補"
        elif result.today > p.rights.last_cum_date:
            verdict = "現渡し待ち"
        else:
            verdict = "見送り"
        w.writerow([
            verdict, p.phase, p.item.code, p.item.name,
            p.rights.record_date.month, p.rights.record_date.isoformat(),
            p.rights.last_cum_date.isoformat(), p.rights.ex_date.isoformat(),
            mp.method.label if mp else "", q.entry_date.isoformat() if q and p.best else "",
            mp.window_start.isoformat() if mp else "",
            mp.breakeven_date.isoformat() if mp and mp.breakeven_date else "",
            q.value if q else "", q.lending_fee if q else "", q.gyakuhibu_expected if q else "",
            q.gyakuhibu_worst if q else "", q.commissions if q else "", q.dividend_cost if q else "",
            q.cost_expected if q else "", q.net_expected if q else "", q.net_worst if q else "",
            p.capital, f"{p.roi:.4f}" if p.roi is not None else "", " / ".join(p.warnings),
        ])
    return buf.getvalue()


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold(line: str) -> List[str]:
    """RFC 5545: 1行75オクテット以内に折り返す。"""
    out, cur = [], ""
    for c in line:
        limit = 75 if not out else 74
        if len((cur + c).encode("utf-8")) > limit:
            out.append(cur)
            cur = c
        else:
            cur += c
    out.append(cur)
    return [out[0]] + [" " + s for s in out[1:]]


def render_ics(result: PlanResult, settings: Settings, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//yutai-cross-planner//JA",
        "CALSCALE:GREGORIAN", "X-WR-CALNAME:優待クロス",
    ]

    def event(day: date, key: str, summary: str, desc: str) -> None:
        nxt = date.fromordinal(day.toordinal() + 1)
        for raw in (
            "BEGIN:VEVENT",
            f"UID:{key}@yutai-cross",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{nxt.strftime('%Y%m%d')}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape(desc)}",
            "END:VEVENT",
        ):
            lines.extend(_ics_fold(raw))

    for p in result.recommended:
        it, rd, mp = p.item, p.rights, p.best
        q = mp.recommended
        if p.position is not None:
            continue  # 建玉の記録があるものは下で記録にもとづいて入れる
        base = f"{it.code}-{it.shares}-{rd.record_date.isoformat()}"
        info = (f"{it.name}（{it.benefit or '優待'}）\n方法: {mp.method.label}\n"
                f"見込み利益: {q.net_expected:,}円\n権利付最終日: {fmt_date(rd.last_cum_date)}\n"
                f"現渡し: {fmt_date(rd.ex_date)}")
        if p.crossed:
            event(rd.ex_date, f"{base}-ex", f"【優待クロス】{it.code} {it.name} 現渡し", info)
            continue
        if q.entry_date != rd.last_cum_date:
            event(q.entry_date, f"{base}-entry", f"【優待クロス】{it.code} {it.name} エントリー目安", info)
        event(rd.last_cum_date, f"{base}-last", f"【優待クロス】{it.code} {it.name} 権利付最終日", info)
        event(rd.ex_date, f"{base}-ex", f"【優待クロス】{it.code} {it.name} 現渡し", info)
        if (mp.method.is_short_term and mp.window_start >= result.today
                and mp.window_start != q.entry_date):
            event(mp.window_start, f"{base}-open", f"【優待クロス】{it.code} {it.name} 短期初日", info)
    for pos in result.open_positions:
        rd = result.cal.rights_dates(pos.record_date)
        due = position_due_date(result, pos)
        info = (f"建玉 #{pos.id}（{pos.method_id}・{fmt_date(pos.entry_date)}建て・{pos.shares:,}株）\n"
                f"権利落ち日に現渡し。終わったら position close {pos.id}"
                + (f"\n返済期日: {fmt_date(due)}" if due else ""))
        event(rd.ex_date, f"pos-{pos.id}-ex", f"【優待クロス】{pos.code} {pos.name} 現渡し（保有中）", info)
        if due is not None and due != rd.ex_date:
            event(due, f"pos-{pos.id}-due", f"【優待クロス】{pos.code} {pos.name} 返済期日", info)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
