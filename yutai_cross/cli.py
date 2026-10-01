"""コマンドライン: python -m yutai_cross <サブコマンド>"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from .config import Settings, normalize_code, load_settings
from .costs import quote
from .jpx_calendar import MarketCalendar, RightsDates, fmt_date, record_date_for
from .planner import (
    actions_on,
    build_plan,
    method_by_id,
    methods_for,
    next_action_day,
    plan_method,
    position_alerts,
    short_due_date,
    short_window_start,
    unknown_brokers,
    upcoming_rights,
)
from .positions import (
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_RECEIVED,
    Position,
    find_position,
    load_positions,
    next_id,
    save_positions,
)
from .prices import fetch_prices
from .report import cost_breakdown, render_console, render_csv, render_ics, render_markdown
from .tax import HEADER_ALIASES as EXECUTED_ALIASES
from .tax import load_executed, tax_memo
from .watchlist import WatchItem, csv_rows, load_price_csv, load_watchlist, nfkc, read_text

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = Path("mydata")
DEFAULT_WATCHLIST = DEFAULT_DIR / "watchlist.csv"
DEFAULT_CONFIG = DEFAULT_DIR / "settings.json"
DEFAULT_POSITIONS = DEFAULT_DIR / "positions.csv"
DEFAULT_EXECUTED = DEFAULT_DIR / "executed.csv"
EXECUTED_HEADERS = ["受取日", "コード", "銘柄名", "優待内容", "評価額", "売却額", "貸株料", "逆日歩",
                    "手数料", "受取配当(税引前)", "配当落調整金", "メモ"]


def _examples_dir() -> Path:
    """サンプルの置き場所（今いるフォルダの examples を優先）。"""
    for p in (Path.cwd() / "examples", ROOT / "examples"):
        if p.exists():
            return p
    return ROOT / "examples"


def _date(s: str) -> date:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"日付は 2026-12-31 の形で入力してください: {s}")


def _nonneg_int(s: str) -> int:
    try:
        v = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"整数で入力してください: {s}") from None
    if v < 0:
        raise argparse.ArgumentTypeError("0 以上の整数を指定してください")
    return v


def _load_common(args: argparse.Namespace):
    config_path: Optional[Path] = args.config
    if config_path is None and DEFAULT_CONFIG.exists():
        config_path = DEFAULT_CONFIG
    settings = load_settings(config_path)
    if getattr(args, "horizon", None) is not None:
        settings.horizon_days = args.horizon
    if getattr(args, "budget", None) is not None:
        settings.capital_budget_yen = args.budget
    cal = MarketCalendar(settings.extra_market_holidays, settings.settlement_days)
    return settings, cal


def _build(args: argparse.Namespace):
    settings, cal = _load_common(args)
    wl_path: Path = args.watchlist
    if not wl_path.exists():
        sys.exit(f"ウォッチリストが見つかりません: {wl_path}\n"
                 f"今いるフォルダ: {Path.cwd()}\n"
                 "README.md があるフォルダで実行しているか確認してください。"
                 "はじめての場合は `python -m yutai_cross init` でサンプルをコピーします。")
    items: List[WatchItem] = load_watchlist(wl_path)
    prices: Dict[str, float] = {}
    if args.prices:
        prices.update(load_price_csv(args.prices))
    if args.fetch_prices:
        print("[株価取得] 米Yahoo Finance の非公開APIを使います。利用規約上、自動取得が認められていない"
              "可能性があります。個人の確認用途に限り、取得したデータは再配布しないでください。", file=sys.stderr)
        fetched, errors = fetch_prices(i.code for i in items if i.enabled)
        prices.update(fetched)
        for e in errors:
            print(f"[株価取得] {e}（ウォッチリストの株価を使います）", file=sys.stderr)
    today = args.today or date.today()
    positions = load_positions(args.positions)
    result = build_plan(items, today, settings, prices, cal, positions)
    return settings, cal, result


def cmd_plan(args: argparse.Namespace) -> int:
    settings, cal, result = _build(args)
    written = []
    if args.out:
        out: Path = args.out
        out.mkdir(parents=True, exist_ok=True)
        stem = f"plan_{result.today.strftime('%Y%m%d')}"
        (out / f"{stem}.md").write_text(render_markdown(result, settings, cal), encoding="utf-8")
        # Excel で文字化けしないよう BOM 付き UTF-8
        (out / f"{stem}.csv").write_text(render_csv(result, settings), encoding="utf-8-sig")
        # iCalendar は CRLF 固定。Windows の改行変換を避けるためバイトで書く
        (out / "yutai_cross.ics").write_bytes(render_ics(result, settings).encode("utf-8"))
        written = [out / f"{stem}.md", out / f"{stem}.csv", out / "yutai_cross.ics"]
    print(render_console(result, settings, cal))
    if written:
        print("\n出力しました: " + ", ".join(str(p) for p in written))
    return 0


def cmd_today(args: argparse.Namespace) -> int:
    settings, cal, result = _build(args)
    today = result.today
    nxt = cal.next_business_day(today)
    found = False
    rates = settings.rates_warning(today)
    alerts = position_alerts(result)
    if rates or alerts:
        print("■ 先に確認してください")
        for line in ([rates] if rates else []) + alerts:
            print(f"  - {line}")
        found = bool(alerts)
    for d in ([today] if cal.is_business_day(today) else []) + [nxt]:
        acts = actions_on(d, result)
        if d == today:
            print(f"■ 今日 {fmt_date(d)}")
        else:
            print(f"■ 次の営業日 {fmt_date(d)}（寄付で約定させる注文は今夜〜当日8:59までに）")
        if not acts:
            print("  （なし）")
        for a in acts:
            found = True
            print(f"  - [{a.kind}] {a.text}")
    if not found:
        upcoming = next_action_day(result, cal, nxt)
        if upcoming:
            day, acts = upcoming
            first = acts[0]
            print(f"■ 次の予定: {fmt_date(day)} [{first.kind}] {first.code} {first.name}"
                  "（その前営業日の夜に today をもう一度実行）")
        else:
            print("■ 当面の予定はありません（ウォッチリストに銘柄を追加してください）")
    return 0 if found or not args.exit_code else 1


def cmd_dates(args: argparse.Namespace) -> int:
    settings, cal = _load_common(args)
    rd = cal.rights_dates(args.record)
    print(f"権利確定日（基準日）: {fmt_date(rd.record_date)}")
    if rd.effective_record_date != rd.record_date:
        print(f"  ※基準日が休場日のため、実質の基準日は {fmt_date(rd.effective_record_date)}")
    print(f"権利付最終日        : {fmt_date(rd.last_cum_date)}  ← この日の取引で買えば権利獲得")
    print(f"権利落ち日          : {fmt_date(rd.ex_date)}  ← この日に現渡し（この日以降。返済期日に注意）")
    for m in settings.enabled_methods():
        if not m.is_short_term:
            continue
        start = short_window_start(cal, rd.ex_date, m)
        print(f"{m.label}（{m.term_label}）の建て可能開始日（短期初日）: {fmt_date(start)}")
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    settings, cal = _load_common(args)
    methods = {m.id: m for m in settings.methods}
    if args.method not in methods:
        sys.exit(f"method は次から選んでください: {', '.join(methods)}")
    if args.price <= 0:
        sys.exit("--price は正の数で指定してください")
    m = methods[args.method]
    item = WatchItem(
        code="-", name="試算", record_months=[args.record.month], shares=args.shares,
        price=args.price, benefit_value=args.benefit, dividend=args.dividend,
        gyakuhibu_est=args.gyakuhibu, taishaku=True,
    )
    rd = cal.rights_dates(args.record)
    entry = args.entry or rd.last_cum_date
    if not cal.is_business_day(entry):
        sys.exit(f"--entry {fmt_date(entry)} は休場日です。営業日を指定してください")
    if entry > rd.last_cum_date:
        sys.exit(f"--entry は権利付最終日 {fmt_date(rd.last_cum_date)} 以前にしてください")
    if m.is_short_term:
        start = short_window_start(cal, rd.ex_date, m)
        if entry < start:
            sys.exit(f"{m.label} は {fmt_date(start)} 以降でないと、返済期日までに権利落ち日の現渡しができません")
    q = quote(item, rd, m, entry, cal, settings)
    print(f"方法: {q.method.label}")
    print(f"クロス日: {fmt_date(entry)} → 現渡し: {fmt_date(rd.ex_date)}（権利付最終日 {fmt_date(rd.last_cum_date)}）")
    print(f"約定代金: {q.notional:,}円")
    print(f"コスト: {cost_breakdown(q)}")
    if m.is_seido:
        basis = "--gyakuhibu の想定値" if args.gyakuhibu is not None else "最高料率×倍率の見積もり"
        print(f"コスト合計: {q.cost_expected:,}円（逆日歩は{basis}。"
              f"最高料率×{settings.seido_worst_multiplier:g}倍なら {q.cost_worst:,}円。臨時措置でさらに増えることもある）")
        print(f"利益: {q.net_expected:,}円（最高料率×{settings.seido_worst_multiplier:g}倍なら {q.net_worst:,}円）")
    else:
        print(f"コスト合計: {q.cost_expected:,}円")
        print(f"利益: {q.net_expected:,}円")
    mp = plan_method(item, rd, m, entry, cal, settings, args.price)
    if mp.breakeven_date:
        print(f"最低利益 {settings.min_profit_yen:,}円 を確保できる最も早いクロス日: {fmt_date(mp.breakeven_date)}")
    return 0


def cmd_tax(args: argparse.Namespace) -> int:
    memo = tax_memo(load_executed(args.executed), args.year)
    print(memo)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(memo, encoding="utf-8")
        print(f"出力しました: {args.out}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    examples = _examples_dir()
    DEFAULT_DIR.mkdir(exist_ok=True)
    # CSV は Excel で文字化けしないよう BOM 付き UTF-8 で作る
    copies = [
        (examples / "watchlist_sample.csv", DEFAULT_WATCHLIST, "utf-8-sig"),
        (examples / "settings.example.json", DEFAULT_CONFIG, "utf-8"),
    ]
    for src, dst, enc in copies:
        if not src.exists():
            sys.exit(f"サンプルが見つかりません: {src}\nREADME.md があるフォルダで実行してください。")
        if dst.exists() and not args.force:
            print(f"既にあるのでスキップ: {dst}")
            continue
        text = read_text(src)
        if dst == DEFAULT_WATCHLIST:
            # サンプルの注意書き（このファイルを編集しない旨）は、自分用のファイルでは不要
            text = "\n".join(l for l in text.splitlines() if not l.startswith("# サンプル")) + "\n"
        dst.write_text(text, encoding=enc)
        print(f"作成: {dst}")
    # 実績ファイルは見出しだけ（架空の実績が確定申告メモに混ざらないように）
    executed = DEFAULT_DIR / "executed.csv"
    if not executed.exists() or args.force:
        sample = examples / "executed_sample.csv"
        header = next(l for l in read_text(sample).splitlines() if l and not l.startswith("#"))
        executed.write_text(header + "\n", encoding="utf-8-sig")
        print(f"作成: {executed}（見出しのみ）")
    else:
        print(f"既にあるのでスキップ: {executed}")
    print(f"\nmydata フォルダ: {DEFAULT_DIR.resolve()}")
    print("次は `python -m yutai_cross plan` でサンプル（架空銘柄）の結果を確認し、"
          "mydata/watchlist.csv を自分の銘柄に書き換えてください。")
    return 0


# ---------- 建玉の記録 ----------

def _watch_item(args: argparse.Namespace, code: str) -> Optional[WatchItem]:
    path: Path = args.watchlist
    if not path.exists():
        return None
    return next((i for i in load_watchlist(path) if i.code == code), None)


def _position_quote(pos: Position, item: Optional[WatchItem], settings: Settings, cal: MarketCalendar):
    m = method_by_id(settings, pos.method_id)
    if m is None:
        return None
    probe = WatchItem(
        code=pos.code, name=pos.name, record_months=[pos.record_date.month], shares=pos.shares,
        price=pos.price, benefit_value=item.benefit_value if item else 0,
        dividend=item.dividend if item else 0.0, taishaku=True,
    )
    rd = cal.rights_dates(pos.record_date)
    return quote(probe, rd, m, min(pos.entry_date, rd.last_cum_date), cal, settings)


def cmd_position_add(args: argparse.Namespace) -> int:
    settings, cal = _load_common(args)
    positions = load_positions(args.positions)
    code = normalize_code(args.code)
    item = _watch_item(args, code)
    m = method_by_id(settings, args.method)
    if m is None:
        sys.exit(f"--method は次から選んでください: {', '.join(x.id for x in settings.methods)}")
    entry = args.date or date.today()
    if not cal.is_business_day(entry):
        sys.exit(f"建日 {fmt_date(entry)} は休場日です。約定した日を --date で指定してください")
    if args.record:
        rd = cal.rights_dates(args.record)
    elif item is not None:
        upcoming = [r for r in upcoming_rights(item, entry, cal) if r.last_cum_date >= entry]
        if not upcoming:
            sys.exit(f"{code} の次の権利日が見つかりません。--record で権利確定日を指定してください")
        rd = upcoming[0]
    else:
        sys.exit(f"{code} はウォッチリストにありません。--record で権利確定日を指定してください")
    if entry > rd.last_cum_date:
        sys.exit(f"建日 {fmt_date(entry)} は権利付最終日 {fmt_date(rd.last_cum_date)} より後です。"
                 "この回の権利は取れません")
    if m.is_short_term and entry < short_window_start(cal, rd.ex_date, m):
        sys.exit(f"{m.label} で {fmt_date(entry)} に建てると、返済期日が権利落ち日より前になります")
    price = args.price if args.price is not None else (item.price if item else None)
    if price is None or price <= 0:
        sys.exit("約定単価を --price で指定してください")
    shares = args.shares or (item.shares if item else 100)
    dup = [p for p in positions
           if p.code == code and p.record_date == rd.record_date and p.status == STATUS_OPEN]
    if dup and not args.force:
        sys.exit(f"{code} の同じ権利日の建玉がすでにあります（#{dup[0].id}）。別に建てたなら --force を付けてください")
    pos = Position(
        id=next_id(positions), code=code, name=item.name if item else code, record_date=rd.record_date,
        method_id=m.id, entry_date=entry, shares=shares, price=price, memo=args.memo or "",
    )
    positions.append(pos)
    save_positions(args.positions, positions)
    q = _position_quote(pos, item, settings, cal)
    due = short_due_date(cal, entry, m) if m.is_short_term else None
    print(f"記録しました: {pos.label}（{m.label}・{shares:,}株・{price:,.0f}円）")
    print(f"  権利付最終日: {fmt_date(rd.last_cum_date)} ／ 現渡し: {fmt_date(rd.ex_date)}"
          + (f" ／ 返済期日: {fmt_date(due)}" if due else ""))
    if q is not None:
        print(f"  見込みコスト: {cost_breakdown(q)}")
    print(f"  ファイル: {args.positions}")
    return 0


def cmd_position_close(args: argparse.Namespace) -> int:
    settings, cal = _load_common(args)
    positions = load_positions(args.positions)
    pos = find_position(positions, args.key, [STATUS_OPEN])
    day = args.date or date.today()
    rd = cal.rights_dates(pos.record_date)
    if day <= rd.last_cum_date and not args.force:
        sys.exit(f"{fmt_date(day)} は権利付最終日（{fmt_date(rd.last_cum_date)}）以前です。この日に現渡しすると"
                 "権利がなくなります。本当に現渡ししたなら --force を付けてください")
    pos.status, pos.close_date = STATUS_CLOSED, day
    save_positions(args.positions, positions)
    print(f"現渡しを記録しました: {pos.label}（{fmt_date(day)}）")
    m = method_by_id(settings, pos.method_id)
    if m is not None and m.is_short_term and day > short_due_date(cal, pos.entry_date, m):
        print("  ※返済期日を過ぎています。強制決済になっていないか証券会社の画面で確認してください")
    print(f"  優待は権利確定日（{rd.record_date.isoformat()}）の2〜3か月後に届くのが一般的です。"
          f"届いたら position received {pos.id} --value 評価額（円）")
    return 0


def _append_executed(path: Path, row: Dict[str, str]) -> None:
    """実績ファイルに1行追記する（既存の見出しの順番に合わせる）。"""
    text = read_text(path) if path.exists() else ""
    rows = csv_rows(text) if text.strip() else []
    if rows:
        header = rows[0][1]
        body = text if text.endswith("\n") else text + "\n"
    else:
        header = EXECUTED_HEADERS
        body = ",".join(EXECUTED_HEADERS) + "\n"
    values = [row.get(EXECUTED_ALIASES.get(nfkc(h), nfkc(h)), "") for h in header]
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.lstrip("﻿") + buf.getvalue(), encoding="utf-8-sig")


def cmd_position_received(args: argparse.Namespace) -> int:
    settings, cal = _load_common(args)
    positions = load_positions(args.positions)
    pos = find_position(positions, args.key, [STATUS_CLOSED, STATUS_OPEN])
    if pos.status == STATUS_OPEN:
        print("※この建玉はまだ「保有中」です。現渡しが済んでいれば position close も実行してください",
              file=sys.stderr)
    item = _watch_item(args, pos.code)
    q = _position_quote(pos, item, settings, cal)
    m = method_by_id(settings, pos.method_id)
    dividend_total = round((item.dividend if item else 0.0) * pos.shares)
    adjustment = round(dividend_total * (m.adj_rate if m else 1.0))
    day = args.date or date.today()
    _append_executed(args.executed, {
        "received": day.isoformat(), "code": pos.code, "name": pos.name,
        "benefit": args.benefit or (item.benefit if item else ""), "value": str(args.value),
        "sold": "" if args.sold is None else str(args.sold),
        "lending": str(q.lending_fee) if q else "", "gyakuhibu": "",
        "commission": str(q.commissions) if q else "",
        "dividend": str(dividend_total) if dividend_total else "",
        "adjustment": str(adjustment) if dividend_total else "",
        "memo": f"建玉#{pos.id}から記録（貸株料・配当は見込み値）",
    })
    pos.status, pos.received_date = STATUS_RECEIVED, day
    save_positions(args.positions, positions)
    print(f"{args.executed} に追記しました: {pos.code} {pos.name} 評価額 {args.value:,}円")
    print("  貸株料・配当・配当落調整金は見込み値です。証券会社の取引報告書の実際の値で直してください")
    return 0


def cmd_position_list(args: argparse.Namespace) -> int:
    settings, cal = _load_common(args)
    positions = load_positions(args.positions)
    shown = [p for p in positions if args.all or p.status != STATUS_RECEIVED]
    if not shown:
        print("記録された建玉はありません（クロスしたら position add で記録します）")
        return 0
    for p in shown:
        rd = cal.rights_dates(p.record_date)
        m = method_by_id(settings, p.method_id)
        due = short_due_date(cal, p.entry_date, m) if m is not None and m.is_short_term else None
        line = (f"#{p.id} {p.code} {p.name} ［{p.status}］ {m.label if m else p.method_id}・{p.shares:,}株・"
                f"{fmt_date(p.entry_date)}建て ／ 権利付最終日 {fmt_date(rd.last_cum_date)}")
        if p.status == STATUS_OPEN:
            line += f" ／ 現渡し {fmt_date(rd.ex_date)}" + (f" ／ 返済期日 {fmt_date(due)}" if due else "")
        elif p.status == STATUS_CLOSED:
            line += f" ／ {fmt_date(p.close_date)} 現渡し済・優待待ち"
        else:
            line += f" ／ {fmt_date(p.received_date)} 優待受取"
        print(line)
    return 0


# ---------- 入力チェック・カレンダー ----------

def cmd_check(args: argparse.Namespace) -> int:
    settings, cal = _load_common(args)
    today = args.today or date.today()
    problems = 0
    print("■ 設定")
    checked = settings.rates_checked_on.isoformat() if settings.rates_checked_on else "未入力"
    print(f"  料率を確認した日: {checked}")
    rates = settings.rates_warning(today)
    if rates:
        print(f"  ！ {rates}")
    for m in settings.methods:
        state = "" if m.enabled else "（無効）"
        print(f"  - {m.id}: {m.label}{state} 貸株料 年{m.lending_rate:.2%} ／ {m.term_label}")
    if settings.capital_budget_yen:
        print(f"  資金枠: {settings.capital_budget_yen:,}円")
    if not args.watchlist.exists():
        sys.exit(f"ウォッチリストが見つかりません: {args.watchlist}")
    row_errors: List[str] = []
    items = load_watchlist(args.watchlist, errors=row_errors)
    if row_errors:
        print("\n■ 読み込めなかった行（直すまでこの銘柄は計算されません）")
        for e in row_errors:
            print(f"  ！ {e}")
        problems += len(row_errors)
    print(f"\n■ ウォッチリスト（{args.watchlist}・{len(items)}銘柄）")
    excluded = set(settings.exclude_codes)
    for it in items:
        issues = list(it.notes)
        unknown = unknown_brokers(it, settings)
        if unknown:
            issues.append(f"「一般信用」列の {'、'.join(unknown)} を読めません")
        methods = methods_for(it, settings)
        if not methods:
            issues.append("売建できる方法がありません（一般信用の列と貸借の列を確認）")
        if it.price is None and not args.prices:
            issues.append("株価が空です（--prices で渡すならこのままでOK）")
        notes = []
        if it.code in excluded:
            notes.append("除外リストに入っています")
        if it.long_term:
            notes.append("長期保有条件あり（初期設定では見送り）")
        if not it.enabled:
            notes.append("無効（×）")
        nxt = upcoming_rights(it, today, cal)
        when = fmt_date(nxt[0].last_cum_date) if nxt else "-"
        mark = "要確認" if issues else "OK"
        problems += bool(issues)
        print(f"  [{mark}] {it.line_no}行目 {it.code} {it.name}: 次の権利付最終日 {when} ／ "
              f"方法 {', '.join(m.id for m in methods) or 'なし'}")
        for x in issues:
            print(f"      ！ {x}")
        for x in notes:
            print(f"      ・{x}")
    positions = load_positions(args.positions)
    if positions:
        print(f"\n■ 建玉の記録（{args.positions}）: 保有中 "
              f"{sum(p.status == STATUS_OPEN for p in positions)}件 ／ 優待待ち "
              f"{sum(p.status == STATUS_CLOSED for p in positions)}件")
    print(f"\n要確認: {problems}件" if problems else "\n問題は見つかりませんでした")
    return 1 if problems else 0


def _month_range(start: date, months: int):
    y, m = start.year, start.month
    for _ in range(months):
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def cmd_calendar(args: argparse.Namespace) -> int:
    settings, cal = _load_common(args)
    start = args.start or date.today().replace(day=1)
    shorts = [m for m in settings.enabled_methods() if m.is_short_term]
    head = "| 権利付最終日 | 権利落ち日 | 権利確定日 | 銘柄 | " + " | ".join(
        f"{m.broker}短期初日" for m in shorts) + " |"
    sep = "|" + "---|" * (4 + len(shorts))

    def row(rd: RightsDates, label: str) -> str:
        cells = [fmt_date(short_window_start(cal, rd.ex_date, m)) for m in shorts]
        return (f"| **{fmt_date(rd.last_cum_date)}** | {fmt_date(rd.ex_date)} | {fmt_date(rd.record_date)} | "
                f"{label} | " + " | ".join(cells) + " |")

    lines = [head, sep]
    if not args.month_end and args.watchlist.exists():
        events = []
        for it in load_watchlist(args.watchlist):
            if not it.enabled:
                continue
            for y, m in _month_range(start, args.months):
                if m in it.record_months:
                    rd = cal.rights_dates(record_date_for(y, m, it.record_day))
                    events.append((rd, f"{it.code} {it.name}"))
        for rd, label in sorted(events, key=lambda e: (e[0].last_cum_date, e[1])):
            lines.append(row(rd, label))
        title = f"ウォッチリストの権利日（{start.year}年{start.month}月から{args.months}か月）"
    else:
        for y, m in _month_range(start, args.months):
            lines.append(row(cal.rights_dates(record_date_for(y, m, "末")), "（月末権利）"))
        title = f"月末権利の日程（{start.year}年{start.month}月から{args.months}か月）"
    text = f"# {title}\n\n" + "\n".join(lines) + "\n"
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"出力しました: {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m yutai_cross",
        description="株主優待クロス取引の候補検出・タイミング判定ツール（試算用。売買の推奨ではありません）",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, watch: bool = True) -> None:
        sp.add_argument("-c", "--config", type=Path, help="設定JSON（省略時 mydata/settings.json → 既定値）")
        if watch:
            sp.add_argument("-w", "--watchlist", type=Path, default=DEFAULT_WATCHLIST,
                            help="ウォッチリストCSV（既定: mydata/watchlist.csv）")
            sp.add_argument("--today", type=_date, help="基準日 YYYY-MM-DD（既定: 今日）")
            sp.add_argument("--horizon", type=_nonneg_int, help="何日先の権利付最終日まで見るか")
            sp.add_argument("--prices", type=Path,
                            help="「コード,株価」のCSVで株価を上書き（証券会社からダウンロードしたものなど）")
            sp.add_argument("--fetch-prices", action="store_true",
                            help="米Yahoo Financeの非公開APIから株価を取得（任意・利用規約に注意）")
            sp.add_argument("--positions", type=Path, default=DEFAULT_POSITIONS,
                            help="建玉の記録（既定: mydata/positions.csv）")
            sp.add_argument("--budget", type=_nonneg_int,
                            help="優待クロスに使える資金の上限（円）。超える分は利回りの低い候補から見送る")

    sp = sub.add_parser("plan", help="候補の一覧と売買スケジュールを出す")
    common(sp)
    sp.add_argument("-o", "--out", type=Path, help="Markdown/CSV/カレンダー(ics)の出力先フォルダ（例: mydata/reports）")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("today", help="今日と次の営業日にやることだけ表示（毎日の自動実行向け）")
    common(sp)
    sp.add_argument("--exit-code", action="store_true", help="やることがなければ終了コード1")
    sp.set_defaults(func=cmd_today)

    sp = sub.add_parser("dates", help="権利確定日から権利付最終日・権利落ち日・短期初日を計算")
    common(sp, watch=False)
    sp.add_argument("record", type=_date, help="権利確定日 YYYY-MM-DD")
    sp.set_defaults(func=cmd_dates)

    sp = sub.add_parser("cost", help="1銘柄のクロスコストを試算")
    common(sp, watch=False)
    sp.add_argument("--record", type=_date, required=True, help="権利確定日 YYYY-MM-DD")
    sp.add_argument("--price", type=float, required=True, help="株価")
    sp.add_argument("--shares", type=int, default=100, help="株数（既定100）")
    sp.add_argument("--benefit", type=int, required=True, help="優待の価値（円）")
    sp.add_argument("--dividend", type=float, default=0.0, help="1株配当（円）")
    sp.add_argument("--gyakuhibu", type=float, help="制度信用の逆日歩想定（円/株/日）")
    sp.add_argument("--method", default="sbi_short", help="売建方法のid（既定 sbi_short）")
    sp.add_argument("--entry", type=_date, help="クロスする日（既定: 権利付最終日）")
    sp.set_defaults(func=cmd_cost)

    sp = sub.add_parser("position", help="建玉の記録（クロスしたら add、現渡ししたら close、優待が届いたら received）")
    psub = sp.add_subparsers(dest="position_command", required=True)

    def pos_common(x: argparse.ArgumentParser) -> None:
        x.add_argument("-c", "--config", type=Path, help="設定JSON")
        x.add_argument("-w", "--watchlist", type=Path, default=DEFAULT_WATCHLIST, help="ウォッチリストCSV")
        x.add_argument("--positions", type=Path, default=DEFAULT_POSITIONS, help="建玉の記録CSV")

    x = psub.add_parser("add", help="クロスした建玉を記録")
    pos_common(x)
    x.add_argument("code", help="銘柄コード")
    x.add_argument("--method", required=True, help="売建方法のid（例: sbi_short, rakuten_short）")
    x.add_argument("--date", type=_date, help="約定日（既定: 今日）")
    x.add_argument("--price", type=float, help="約定単価（既定: ウォッチリストの株価）")
    x.add_argument("--shares", type=int, help="株数（既定: ウォッチリストの必要株数）")
    x.add_argument("--record", type=_date, help="権利確定日（既定: ウォッチリストから自動）")
    x.add_argument("--memo", help="メモ")
    x.add_argument("--force", action="store_true", help="同じ権利日の建玉があっても記録する")
    x.set_defaults(func=cmd_position_add)

    x = psub.add_parser("close", help="現渡しした建玉を記録")
    pos_common(x)
    x.add_argument("key", help="建玉のID（#なし）か銘柄コード")
    x.add_argument("--date", type=_date, help="現渡しした日（既定: 今日）")
    x.add_argument("--force", action="store_true", help="権利付最終日以前の日付でも記録する")
    x.set_defaults(func=cmd_position_close)

    x = psub.add_parser("received", help="優待が届いたら記録し、実績CSVに追記")
    pos_common(x)
    x.add_argument("key", help="建玉のID（#なし）か銘柄コード")
    x.add_argument("--value", type=int, required=True, help="評価額（受け取った時の時価・円）")
    x.add_argument("--sold", type=int, help="売却額（売った場合・円）")
    x.add_argument("--benefit", help="優待内容（既定: ウォッチリストの優待内容）")
    x.add_argument("--date", type=_date, help="受取日（既定: 今日）")
    x.add_argument("--executed", type=Path, default=DEFAULT_EXECUTED, help="実績CSV")
    x.set_defaults(func=cmd_position_received)

    x = psub.add_parser("list", help="建玉の一覧（受取済みは --all で表示）")
    pos_common(x)
    x.add_argument("--all", action="store_true", help="受取済みも表示")
    x.set_defaults(func=cmd_position_list)

    sp = sub.add_parser("check", help="ウォッチリストと設定の入力チェック")
    common(sp)
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("calendar", help="権利付最終日・権利落ち日・短期初日の一覧（ウォッチリストまたは月末）")
    common(sp)
    sp.add_argument("--start", type=_date, help="開始日（既定: 今月1日）")
    sp.add_argument("--months", type=_nonneg_int, default=12, help="何か月分（既定12）")
    sp.add_argument("--month-end", action="store_true", help="ウォッチリストではなく月末権利の一覧を出す")
    sp.add_argument("-o", "--out", type=Path, help="Markdownの出力先ファイル")
    sp.set_defaults(func=cmd_calendar)

    sp = sub.add_parser("tax", help="実績CSVから確定申告用メモを作る")
    sp.add_argument("--executed", type=Path, default=DEFAULT_DIR / "executed.csv")
    sp.add_argument("--year", type=int, default=date.today().year)
    sp.add_argument("-o", "--out", type=Path, help="Markdownの出力先ファイル")
    sp.set_defaults(func=cmd_tax)

    sp = sub.add_parser("init", help="mydata/ にサンプルのウォッチリストと設定をコピー")
    sp.add_argument("--force", action="store_true", help="既存ファイルを上書き")
    sp.set_defaults(func=cmd_init)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    # Windows でリダイレクトやタスクスケジューラ経由だと出力が cp932 になり、
    # 表せない文字があるとレポート全体が出なくなるので、置き換えて出す
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, csv.Error) as e:
        print(f"エラー: {e}", file=sys.stderr)
    except FileNotFoundError as e:
        print(f"エラー: ファイルが見つかりません: {e.filename}\n今いるフォルダ: {Path.cwd()}\n"
              "README.md があるフォルダで実行しているか、ファイル名が正しいか確認してください。", file=sys.stderr)
    except PermissionError as e:
        print(f"エラー: {e.filename} を読み書きできません。Excel などで開いていれば閉じてから"
              "もう一度実行してください。", file=sys.stderr)
    except OSError as e:
        print(f"エラー: {e.filename or ''} {e.strerror or e}", file=sys.stderr)
    except KeyboardInterrupt:
        return 130
    return 2
