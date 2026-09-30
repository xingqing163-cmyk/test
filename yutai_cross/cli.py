"""コマンドライン: python -m yutai_cross <サブコマンド>"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from .config import load_settings
from .costs import quote
from .jpx_calendar import MarketCalendar, fmt_date
from .planner import actions_on, build_plan, next_action_day, plan_method, short_window_start
from .prices import fetch_prices
from .report import cost_breakdown, render_console, render_csv, render_ics, render_markdown
from .tax import load_executed, tax_memo
from .watchlist import WatchItem, load_price_csv, load_watchlist, read_text

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = Path("mydata")
DEFAULT_WATCHLIST = DEFAULT_DIR / "watchlist.csv"
DEFAULT_CONFIG = DEFAULT_DIR / "settings.json"


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
    result = build_plan(items, today, settings, prices, cal)
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
            print(f"■ 次の予定: {fmt_date(day)} [{first.kind}] {first.plan.item.code} {first.plan.item.name}"
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
