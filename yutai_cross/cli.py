"""コマンドライン: python -m yutai_cross <サブコマンド>"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from .config import load_settings
from .costs import quote
from .jpx_calendar import MarketCalendar, fmt_date
from .planner import actions_on, build_plan, plan_method
from .prices import fetch_prices
from .report import render_console, render_csv, render_ics, render_markdown, cost_breakdown
from .tax import load_executed, tax_memo
from .watchlist import WatchItem, load_price_csv, load_watchlist

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
DEFAULT_DIR = Path("mydata")
DEFAULT_WATCHLIST = DEFAULT_DIR / "watchlist.csv"
DEFAULT_CONFIG = DEFAULT_DIR / "settings.json"


def _date(s: str) -> date:
    return date.fromisoformat(s)


def _load_common(args: argparse.Namespace):
    config_path: Optional[Path] = args.config
    if config_path is None and DEFAULT_CONFIG.exists():
        config_path = DEFAULT_CONFIG
    settings = load_settings(config_path)
    if getattr(args, "horizon", None):
        settings.horizon_days = args.horizon
    cal = MarketCalendar(settings.extra_market_holidays, settings.settlement_days)
    return settings, cal


def _build(args: argparse.Namespace):
    settings, cal = _load_common(args)
    wl_path: Path = args.watchlist
    if not wl_path.exists():
        sys.exit(f"ウォッチリストが見つかりません: {wl_path}\n"
                 "まず `python -m yutai_cross init` でサンプルをコピーしてください。")
    items: List[WatchItem] = load_watchlist(wl_path)
    prices: Dict[str, float] = {}
    if args.prices:
        prices.update(load_price_csv(args.prices))
    if args.fetch_prices:
        fetched, errors = fetch_prices(i.code for i in items if i.enabled)
        prices.update(fetched)
        for e in errors:
            print(f"[株価取得] {e}（ウォッチリストの株価を使います）", file=sys.stderr)
    today = args.today or date.today()
    result = build_plan(items, today, settings, prices, cal)
    return settings, cal, result


def cmd_plan(args: argparse.Namespace) -> int:
    settings, cal, result = _build(args)
    print(render_console(result, settings, cal))
    if args.out:
        out: Path = args.out
        out.mkdir(parents=True, exist_ok=True)
        stem = f"plan_{result.today.strftime('%Y%m%d')}"
        (out / f"{stem}.md").write_text(render_markdown(result, settings, cal), encoding="utf-8")
        # Excel で文字化けしないよう BOM 付き UTF-8
        (out / f"{stem}.csv").write_text(render_csv(result, settings), encoding="utf-8-sig")
        (out / "yutai_cross.ics").write_text(render_ics(result, settings), encoding="utf-8")
        print(f"\n出力しました: {out / (stem + '.md')}, {out / (stem + '.csv')}, {out / 'yutai_cross.ics'}")
    return 0


def cmd_today(args: argparse.Namespace) -> int:
    settings, cal, result = _build(args)
    today = result.today
    days = [today] if cal.is_business_day(today) else []
    days.append(cal.next_business_day(today))
    found = False
    for d in days:
        acts = actions_on(d, result)
        label = "今日" if d == today else "次の営業日"
        print(f"■ {label} {fmt_date(d)}")
        if not acts:
            print("  （なし）")
        for a in acts:
            found = True
            print(f"  - [{a.kind}] {a.text}")
    return 0 if found or not args.exit_code else 1


def cmd_dates(args: argparse.Namespace) -> int:
    settings, cal = _load_common(args)
    rd = cal.rights_dates(args.record)
    print(f"権利確定日（基準日）: {fmt_date(rd.record_date)}")
    if rd.effective_record_date != rd.record_date:
        print(f"  ※基準日が休場日のため、実質の基準日は {fmt_date(rd.effective_record_date)}")
    print(f"権利付最終日        : {fmt_date(rd.last_cum_date)}  ← この日の取引で買えば権利獲得")
    print(f"権利落ち日          : {fmt_date(rd.ex_date)}  ← この日以降に現渡し")
    for m in settings.enabled_methods():
        if m.is_seido or m.max_hold_business_days is None:
            continue
        start = cal.add_business_days(rd.ex_date, -m.max_hold_business_days)
        print(f"{m.label} の建て可能開始日（短期初日）: {fmt_date(start)}")
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    settings, cal = _load_common(args)
    methods = {m.id: m for m in settings.methods}
    if args.method not in methods:
        sys.exit(f"method は次から選んでください: {', '.join(methods)}")
    item = WatchItem(
        code="-", name="試算", record_months=[args.record.month], shares=args.shares,
        price=args.price, benefit_value=args.benefit, dividend=args.dividend,
        gyakuhibu_est=args.gyakuhibu, taishaku=True,
    )
    rd = cal.rights_dates(args.record)
    entry = args.entry or rd.last_cum_date
    q = quote(item, rd, methods[args.method], entry, cal, settings)
    print(f"方法: {q.method.label}")
    print(f"クロス日: {fmt_date(entry)} → 現渡し: {fmt_date(rd.ex_date)}（権利付最終日 {fmt_date(rd.last_cum_date)}）")
    print(f"約定代金: {q.notional:,}円")
    print(f"コスト: {cost_breakdown(q)}")
    print(f"コスト合計: {q.cost_expected:,}円（最悪ケース {q.cost_worst:,}円）")
    print(f"利益: {q.net_expected:,}円（最悪ケース {q.net_worst:,}円）")
    mp = plan_method(item, rd, methods[args.method], entry, cal, settings, args.price)
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
    DEFAULT_DIR.mkdir(exist_ok=True)
    pairs = [
        (EXAMPLES / "watchlist_sample.csv", DEFAULT_WATCHLIST),
        (EXAMPLES / "settings.example.json", DEFAULT_CONFIG),
        (EXAMPLES / "executed_sample.csv", DEFAULT_DIR / "executed.csv"),
    ]
    for src, dst in pairs:
        if not src.exists():
            sys.exit(f"サンプルが見つかりません: {src}\nリポジトリを clone したフォルダで実行してください。")
        if dst.exists() and not args.force:
            print(f"既にあるのでスキップ: {dst}")
            continue
        shutil.copyfile(src, dst)
        print(f"作成: {dst}")
    print("次は mydata/watchlist.csv を編集して `python -m yutai_cross plan` を実行してください。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m yutai_cross",
        description="株主優待クロス取引の候補検出・タイミング判定ツール",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, watch: bool = True) -> None:
        sp.add_argument("-c", "--config", type=Path, help="設定JSON（省略時 mydata/settings.json → 既定値）")
        if watch:
            sp.add_argument("-w", "--watchlist", type=Path, default=DEFAULT_WATCHLIST,
                            help="ウォッチリストCSV（既定: mydata/watchlist.csv）")
            sp.add_argument("--today", type=_date, help="基準日 YYYY-MM-DD（既定: 今日）")
            sp.add_argument("--horizon", type=int, help="何日先の権利付最終日まで見るか")
            sp.add_argument("--prices", type=Path, help="「コード,株価」のCSVで株価を上書き")
            sp.add_argument("--fetch-prices", action="store_true",
                            help="Yahoo!ファイナンスから株価を取得（非公式・失敗時はCSVの値）")

    sp = sub.add_parser("plan", help="候補ランキングと売買スケジュールを出す")
    common(sp)
    sp.add_argument("-o", "--out", type=Path, help="Markdown/CSV/カレンダー(ics)の出力先フォルダ")
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
    sp.add_argument("--benefit", type=int, required=True, help="優待の評価額（円）")
    sp.add_argument("--dividend", type=float, default=0.0, help="1株配当（円）")
    sp.add_argument("--gyakuhibu", type=float, help="制度信用の逆日歩想定（円/株/日）")
    sp.add_argument("--method", default="sbi_short", help="売建方法のid（既定 sbi_short）")
    sp.add_argument("--entry", type=_date, help="クロスする日（既定: 権利付最終日）")
    sp.set_defaults(func=cmd_cost)

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
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
