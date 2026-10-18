"""コマンドライン: python -m kaitori_check <サブコマンド>"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .buyback import (
    BuybackItem,
    FetchLog,
    decode_html,
    dedupe,
    fetch_site,
    items_to_csv,
    parse_items_csv,
    parse_listing,
    robots_allows,
)
from .config import Settings, Site, load_settings
from .profit import JUDGE_GOOD, Result, calc, check_item, judge
from .report import breakdown_lines, render_console, render_csv, render_html, signed
from .yahoo import YahooError, search

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = Path("mydata")
DEFAULT_CONFIG = DEFAULT_DIR / "kaitori_settings.json"
DEFAULT_LIST = DEFAULT_DIR / "kaitori_list.csv"
DEFAULT_REPORTS = DEFAULT_DIR / "reports"
DEFAULT_RAW = DEFAULT_DIR / "kaitori_raw"

APPID_HELP = ("Yahoo!ショッピングAPIのアプリケーションID（Client ID）が未設定です。\n"
              "Yahoo!デベロッパーネットワークで発行し、mydata/kaitori_settings.json の yahoo_appid に書くか、"
              "環境変数 YAHOO_APPID に設定してください。手順は docs/kaitori/README.md にあります。")


def _examples_dir() -> Path:
    for p in (Path.cwd() / "examples", ROOT / "examples"):
        if p.exists():
            return p
    return ROOT / "examples"


def _nonneg_int(s: str) -> int:
    try:
        v = int(s.replace(",", ""))
    except ValueError:
        raise argparse.ArgumentTypeError(f"整数で入力してください: {s}") from None
    if v < 0:
        raise argparse.ArgumentTypeError("0 以上の整数を指定してください")
    return v


def _settings(args: argparse.Namespace) -> Settings:
    path: Optional[Path] = args.config
    if path is None and DEFAULT_CONFIG.exists():
        path = DEFAULT_CONFIG
    return load_settings(path)


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Excel で「CSV（コンマ区切り）」保存すると Shift_JIS になる
        return raw.decode("cp932")


def load_list(path: Path) -> List[BuybackItem]:
    if not path.exists():
        sys.exit(f"買取価格リストが見つかりません: {path}\n"
                 "先に `python -m kaitori_check fetch` で買取サイトから取得するか、CSVを用意して -i で指定してください。")
    return parse_items_csv(_read_text(path))


def cmd_init(args: argparse.Namespace) -> int:
    src = _examples_dir() / "kaitori_settings.example.json"
    if not src.exists():
        sys.exit(f"サンプルが見つかりません: {src}\nREADME.md があるフォルダで実行してください。")
    DEFAULT_DIR.mkdir(exist_ok=True)
    if DEFAULT_CONFIG.exists() and not args.force:
        print(f"既にあるのでスキップ: {DEFAULT_CONFIG}")
    else:
        DEFAULT_CONFIG.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"作成: {DEFAULT_CONFIG}")
    print("\n次の手順:\n"
          "  1. mydata/kaitori_settings.json の yahoo_appid に Client ID を書く（docs/kaitori/README.md 参照）\n"
          "  2. python -m kaitori_check fetch   … 買取サイトの価格一覧を mydata/kaitori_list.csv に保存\n"
          "  3. python -m kaitori_check check   … ヤフショで仕入れて黒字になる商品を判定")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    s = _settings(args)
    items: List[BuybackItem] = []
    if args.html:
        for path in args.html:
            html = decode_html(path.read_bytes())
            parsed, parser = parse_listing(html, args.site or "保存したページ", args.url or "")
            for it in parsed:
                it.category = args.category or ""
                it.fetched_at = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            print(f"{path}: {len(parsed)}件（読み方: {parser or '読めず'}）", file=sys.stderr)
            items.extend(parsed)
        items = dedupe(items)
    else:
        sites = s.enabled_sites()
        if args.url:
            sites = [Site(name=args.site or "指定URL", url=args.url, category=args.category or "")]
        elif args.site:
            sites = [x for x in sites if x.name == args.site]
            if not sites:
                sys.exit(f"設定の sites に「{args.site}」がありません")
        if not sites:
            sys.exit("取得する買取サイトがありません。設定の sites にURLを書くか、--url で指定してください")

        for site in sites:
            print(f"[{site.name}] {site.category} {site.url}", file=sys.stderr)
            if not robots_allows(site.url):
                print(f"[{site.name}] robots.txt で自動取得が禁止されている（または robots.txt の取得を拒否された）"
                      "ため取得しません。"
                      "ブラウザでページを保存して --html で読み込んでください。", file=sys.stderr)
                continue
            raw_dir = DEFAULT_RAW / datetime.now().strftime("%Y%m%d_%H%M%S")

            def save_raw(n: int, html: str, _site: str = site.name) -> None:
                if args.save_raw:
                    raw_dir.mkdir(parents=True, exist_ok=True)
                    (raw_dir / f"{_site}_{n:02}.html").write_text(html, encoding="utf-8")

            def on_page(log: FetchLog, _site: str = site.name) -> None:
                print(f"  {log.url}: {log.count}件（新規{log.new}件・読み方: {log.parser or '読めず'}）",
                      file=sys.stderr)

            try:
                got = fetch_site(site.name, site.url, site.category, max_pages=s.max_pages,
                                 interval=s.site_interval_sec, on_page=on_page, save_raw=save_raw)
            except OSError as e:
                print(f"[{site.name}] 取得できませんでした（{e}）。ブラウザでページを保存して --html で"
                      "読み込むこともできます。", file=sys.stderr)
                continue
            if not got:
                print(f"[{site.name}] 商品を読み取れませんでした。JavaScriptで表示するサイトの可能性があります。"
                      "ブラウザで一覧を表示し「名前を付けて保存（ウェブページ、完全）」で保存したファイルを"
                      "--html で渡してください。--save-raw を付けると取得したHTMLを mydata/kaitori_raw に保存します。",
                      file=sys.stderr)
            items.extend(got)
    if not items:
        print("買取価格を1件も取得できませんでした。", file=sys.stderr)
        return 1
    if args.min_price:
        items = [i for i in items if i.price >= args.min_price]
    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    # Excel で文字化けしないよう BOM 付き UTF-8
    out.write_text(items_to_csv(items), encoding="utf-8-sig")
    print(f"{len(items)}件を保存しました: {out}")
    for it in items[:5]:
        print(f"  {it.price:>10,}円  {it.jan or '-':13}  {it.name}  {it.condition}")
    if len(items) > 5:
        print("  …")
    print("商品名・価格が正しく読めているか、CSVを開いて確認してから check を実行してください。")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    s = _settings(args)
    appid = s.appid()
    if not appid:
        sys.exit(APPID_HELP)
    items = load_list(args.input)
    if args.site:
        items = [i for i in items if i.site == args.site]
    if args.keyword:
        items = [i for i in items if all(k.lower() in i.name.lower() for k in args.keyword)]
    if args.min_price:
        items = [i for i in items if i.price >= args.min_price]
    items.sort(key=lambda i: -i.price)
    if args.limit:
        items = items[:args.limit]
    if not items:
        sys.exit("条件に合う買取商品がありません")

    last = [0.0]

    def throttled_search(**kw):
        wait = s.yahoo_interval_sec - (time.monotonic() - last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            return search(appid, results=s.yahoo_results, premium=s.lyp_premium, **kw)
        finally:
            last[0] = time.monotonic()

    print(f"{len(items)}商品をヤフショで検索します（1件あたり約{s.yahoo_interval_sec:g}秒）", file=sys.stderr)
    results: List[Result] = []
    for n, item in enumerate(items, start=1):
        try:
            r = check_item(item, s, throttled_search)
        except YahooError as e:
            msg = str(e)
            if "アプリケーションID" in msg or "429" in msg:
                print(f"ヤフショAPIのエラー: {msg}", file=sys.stderr)
                if not results:
                    return 1
                print("ここまでの結果を出力します。", file=sys.stderr)
                break
            r = Result(item=item, note=f"検索エラー: {msg}")
        results.append(r)
        tail = f"{r.judge} {signed(r.breakdown.profit)}円" if r.breakdown else (r.note or r.judge)
        print(f"[{n}/{len(items)}] {item.name[:40]} … {tail}", file=sys.stderr)

    print(render_console(results, s, show=args.show, show_loss=args.show_loss))
    if not args.no_files:
        out: Path = args.out
        out.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        stem = f"kaitori_{now:%Y%m%d_%H%M}"
        (out / f"{stem}.csv").write_text(render_csv(results), encoding="utf-8-sig")
        (out / f"{stem}.html").write_text(render_html(results, s, now), encoding="utf-8")
        print(f"\n出力しました: {out / (stem + '.csv')}, {out / (stem + '.html')}")
    return 0


def cmd_calc(args: argparse.Namespace) -> int:
    s = _settings(args)
    points = args.points
    if points is None:
        points = int(args.buy * 100 // 110 * args.point_rate)
    b = calc(args.sell, args.buy, s, api_points=points, shipping_in=args.shipping)
    print(breakdown_lines(b, s))
    print(f"\n判定: {judge(b, s)}（{s.min_profit_yen:,}円以上かつ利益率{s.min_profit_rate:.0%}以上で{JUDGE_GOOD}）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kaitori_check",
        description="買取サイトの価格とヤフショの販売価格を比べ、仕入れて黒字になる商品を探す",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def cfg(x: argparse.ArgumentParser) -> None:
        x.add_argument("-c", "--config", type=Path, help="設定JSON（既定: mydata/kaitori_settings.json）")

    sp = sub.add_parser("init", help="mydata/ に設定ファイルのひな形をコピー")
    sp.add_argument("--force", action="store_true", help="既存ファイルを上書き")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("fetch", help="買取サイトの価格一覧を取得してCSVに保存")
    cfg(sp)
    sp.add_argument("--site", help="設定の sites のうち、この名前のサイトだけ取得（--url/--html と使うとサイト名）")
    sp.add_argument("--url", help="設定にないURLを取得（--html と使うときはリンクの基準URL）")
    sp.add_argument("--category", help="カテゴリ名（--url / --html のとき）")
    sp.add_argument("--html", type=Path, nargs="+", help="ブラウザで保存したHTMLファイルから読み込む")
    sp.add_argument("--min-price", type=_nonneg_int, help="この買取価格（円）未満の商品は保存しない")
    sp.add_argument("-o", "--out", type=Path, default=DEFAULT_LIST, help="保存先CSV（既定: mydata/kaitori_list.csv）")
    sp.add_argument("--save-raw", action="store_true", help="取得したHTMLを mydata/kaitori_raw に保存（読み取りの調査用）")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("check", help="買取価格リストの各商品をヤフショで探し、利益を判定")
    cfg(sp)
    sp.add_argument("-i", "--input", type=Path, default=DEFAULT_LIST, help="買取価格リストCSV（既定: mydata/kaitori_list.csv）")
    sp.add_argument("--site", help="このサイトの商品だけ")
    sp.add_argument("-k", "--keyword", nargs="+", help="商品名にこの言葉を含むものだけ（複数はすべて含むもの）")
    sp.add_argument("--min-price", type=_nonneg_int, help="この買取価格（円）以上の商品だけ")
    sp.add_argument("--limit", type=_nonneg_int, help="買取価格の高い順に最大何件まで調べるか")
    sp.add_argument("--show", type=_nonneg_int, default=30, help="画面に表示する件数（既定30）")
    sp.add_argument("--show-loss", action="store_true", help="赤字の商品も画面に表示")
    sp.add_argument("-o", "--out", type=Path, default=DEFAULT_REPORTS, help="CSV/HTMLの出力先（既定: mydata/reports）")
    sp.add_argument("--no-files", action="store_true", help="CSV/HTMLを出力しない")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("calc", help="1商品の利益を手入力で計算")
    cfg(sp)
    sp.add_argument("--sell", type=_nonneg_int, required=True, help="買取価格（円）")
    sp.add_argument("--buy", type=_nonneg_int, required=True, help="仕入価格（税込・円）")
    sp.add_argument("--points", type=_nonneg_int, help="もらえるポイント数（省略時は --point-rate から計算）")
    sp.add_argument("--point-rate", type=float, default=0.01,
                    help="ポイント率（税抜価格に対する割合。既定0.01。設定の extra_point_rate はさらに上乗せ）")
    sp.add_argument("--shipping", type=_nonneg_int, default=0, help="仕入れの送料（円）")
    sp.set_defaults(func=cmd_calc)
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
