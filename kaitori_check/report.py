"""判定結果の出力（端末表示・CSV・HTML）。"""

from __future__ import annotations

import csv
import html
import io
import unicodedata
from datetime import datetime
from typing import List, Sequence

from .buyback import excel_safe
from .config import TAX_MODE_LABELS, Settings
from .profit import JUDGE_GOOD, JUDGE_LOSS, JUDGE_NONE, JUDGE_THIN, Breakdown, Result

DISCLAIMER = ("※ 取得時点の価格にもとづく試算です。買取価格・販売価格・在庫・ポイントはすぐ変わり、"
              "買取は到着時の検品で減額されることがあります。仕入れ前に両方のページで最新の条件を確認してください。")


def yen(v: int) -> str:
    return f"{v:,}"


def signed(v: int) -> str:
    return f"{v:+,}"


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F", "A") else 1 for c in s)


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


def settings_summary(s: Settings) -> str:
    pts = "含める" if s.judge_with_points else "含めない（現金のみ）"
    tax = TAX_MODE_LABELS[s.tax_mode]
    return (f"判定: 利益{s.min_profit_yen:,}円以上かつ利益率{s.min_profit_rate:.0%}以上で{JUDGE_GOOD}"
            f" / ポイント: {pts}（上乗せ{s.extra_point_rate:.1%}・1pt={s.point_value:g}円"
            f"{'・LYPプレミアム' if s.lyp_premium else ''}）"
            f" / 消費税: {tax} / 発送送料{s.send_shipping_yen:,}円・振込手数料{s.transfer_fee_yen:,}円")


def counts(results: List[Result]) -> str:
    c = {j: 0 for j in (JUDGE_GOOD, JUDGE_THIN, JUDGE_LOSS, JUDGE_NONE)}
    for r in results:
        c[r.judge] += 1
    return (f"{len(results)}商品: {JUDGE_GOOD} {c[JUDGE_GOOD]} / {JUDGE_THIN} {c[JUDGE_THIN]} / "
            f"{JUDGE_LOSS} {c[JUDGE_LOSS]} / ヤフショで見つからず {c[JUDGE_NONE]}")


def render_console(results: List[Result], s: Settings, show: int = 30, show_loss: bool = False) -> str:
    rows = []
    shown = [r for r in sorted(results, key=lambda r: r.sort_key)
             if r.breakdown is not None and (show_loss or r.judge != JUDGE_LOSS)][:show]
    for r in shown:
        b, o = r.breakdown, r.offer
        assert b is not None and o is not None
        rate = f"{b.profit / b.outlay:.1%}" if b.outlay else "-"
        rows.append([r.judge, signed(b.profit), signed(b.cash_profit), rate, yen(r.item.price), yen(o.price),
                     yen(b.points), "JAN" if r.match == "JAN" else "名前", r.item.name, o.store])
    out = [counts(results), settings_summary(s), ""]
    if rows:
        out.append(_table(["判定", "利益", "現金利益", "利益率", "買取価格", "ヤフショ", "ポイント", "一致", "商品名",
                           "ストア"], rows, [6, 9, 9, 6, 9, 9, 8, 4, 34, 16], right_cols=(1, 2, 3, 4, 5, 6)))
        if len(rows) < len([r for r in results if r.breakdown is not None]):
            out.append(f"（ほかは省略。--show・--show-loss で表示数を変更、全件はCSV/HTMLに出力）")
        out.append("")
        out.append("「一致=名前」は商品名からの推定です。型番・色・キット・付属品が同じか、必ずページで確認してください。")
        cautions = [(r.item.name, c) for r in shown if r.judge != JUDGE_LOSS for c in r.cautions]
        if cautions:
            out.append("")
            out.append("■ 注意")
            for name, c in cautions:
                out.append(f"  - {_fit(name, 40).rstrip()}: {c}")
    else:
        out.append("黒字・薄利の候補はありませんでした。")
    out.append("")
    out.append(DISCLAIMER)
    return "\n".join(out)


def breakdown_lines(b: Breakdown, s: Settings) -> str:
    lines = [
        f"  買取価格             {yen(b.sell):>12} 円",
        f"  仕入価格（税込）     {yen(-b.buy):>12} 円",
        f"  仕入れの送料         {yen(-b.shipping_in):>12} 円",
        f"  買取サイトへの送料   {yen(-b.shipping_out):>12} 円",
        f"  振込手数料           {yen(-b.transfer_fee):>12} 円",
        f"  その他経費           {yen(-b.other):>12} 円",
        f"  消費税の増加分       {yen(-b.consumption_tax):>12} 円  （{TAX_MODE_LABELS[s.tax_mode]}）",
        f"  ─────────────────────────────",
        f"  現金利益             {signed(b.cash_profit):>12} 円",
        f"  ポイント             {signed(b.point_yen):>12} 円  （{yen(b.points)}pt × {s.point_value:g}円）",
        f"  利益（ポイント込み） {signed(b.profit):>12} 円",
    ]
    if b.outlay:
        lines.append(f"  利益率               {b.profit / b.outlay:>12.1%}    （仕入価格＋送料に対して）")
    return "\n".join(lines)


CSV_HEADERS = ["判定", "利益", "現金利益", "利益率", "買取価格", "ヤフショ価格", "仕入送料", "ポイント",
               "消費税増加分", "一致", "買取サイト", "カテゴリ", "商品名(買取)", "JAN", "区分", "商品名(ヤフショ)",
               "ストア", "送料表示", "注意", "備考", "買取URL", "ヤフショURL", "候補数", "除外数"]


def render_csv(results: List[Result]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADERS)
    for r in sorted(results, key=lambda r: r.sort_key):
        b, o, it = r.breakdown, r.offer, r.item
        w.writerow([excel_safe(x) for x in [
            r.judge,
            b.profit if b else "", b.cash_profit if b else "",
            f"{b.profit / b.outlay:.3f}" if b and b.outlay else "",
            it.price, o.price if o else "", b.shipping_in if b else "", b.points if b else "",
            b.consumption_tax if b else "", r.match if o else "",
            it.site, it.category, it.name, it.jan, it.condition,
            o.name if o else "", o.store if o else "", o.shipping_label if o else "",
            " / ".join(r.cautions), r.note, it.url, o.url if o else "", r.candidates, r.excluded,
        ]])
    return buf.getvalue()


_CLASS = {JUDGE_GOOD: "good", JUDGE_THIN: "thin", JUDGE_LOSS: "loss", JUDGE_NONE: "none"}


def _link(url: str, text: str) -> str:
    if not url.startswith(("https://", "http://")):
        return html.escape(text)
    return f'<a href="{html.escape(url)}" target="_blank" rel="noopener noreferrer">{html.escape(text)}</a>'


def render_html(results: List[Result], s: Settings, generated: datetime) -> str:
    rows = []
    for r in sorted(results, key=lambda r: r.sort_key):
        b, o, it = r.breakdown, r.offer, r.item
        rate = f"{b.profit / b.outlay:.1%}" if b and b.outlay else ""
        notes = " / ".join(r.cautions + ([r.note] if r.note else []))
        rows.append(
            f'<tr class="{_CLASS[r.judge]}"><td>{html.escape(r.judge)}</td>'
            f'<td class="n">{signed(b.profit) if b else ""}</td>'
            f'<td class="n">{signed(b.cash_profit) if b else ""}</td><td class="n">{rate}</td>'
            f'<td class="n">{yen(it.price)}</td><td class="n">{yen(o.price) if o else ""}</td>'
            f'<td class="n">{yen(b.points) if b else ""}</td><td>{html.escape(r.match if o else "")}</td>'
            f'<td>{_link(it.url, it.name)}<div class="sub">{html.escape(it.site)} {html.escape(it.jan)}</div></td>'
            f'<td>{_link(o.url, o.name) if o else ""}<div class="sub">{html.escape(o.store) if o else ""}</div></td>'
            f'<td class="note">{html.escape(notes)}</td></tr>'
        )
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>買取差益チェック {generated:%Y-%m-%d %H:%M}</title>
<style>
:root {{ --bg:#fff; --fg:#1d1d1f; --sub:#666; --line:#ddd; --good:#e6f6ea; --thin:#fff7e0; --loss:#fbeaea; --none:#f3f3f3; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#16171a; --fg:#e8e8e8; --sub:#9a9a9a; --line:#333; --good:#17351f; --thin:#3a3216; --loss:#3d1d1d; --none:#222; }}
}}
body {{ background:var(--bg); color:var(--fg); font-family:system-ui,-apple-system,"Hiragino Sans","Yu Gothic UI",sans-serif;
  margin:16px; font-size:14px; }}
table {{ border-collapse:collapse; width:100%; }}
th, td {{ border-bottom:1px solid var(--line); padding:6px 8px; vertical-align:top; text-align:left; }}
th {{ position:sticky; top:0; background:var(--bg); }}
td.n {{ text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }}
td.note, .sub {{ color:var(--sub); font-size:12px; }}
tr.good {{ background:var(--good); }} tr.thin {{ background:var(--thin); }}
tr.loss {{ background:var(--loss); }} tr.none {{ background:var(--none); }}
a {{ color:inherit; }}
.wrap {{ overflow-x:auto; }}
</style></head><body>
<h1>買取差益チェック</h1>
<p>{html.escape(generated.strftime('%Y-%m-%d %H:%M'))} 作成 ／ {html.escape(counts(results))}</p>
<p class="sub">{html.escape(settings_summary(s))}</p>
<p class="sub">一致=「名前(要確認)」は商品名からの推定です。型番・色・キット・付属品が同じか必ず確認してください。{html.escape(DISCLAIMER)}</p>
<div class="wrap"><table>
<thead><tr><th>判定</th><th>利益</th><th>現金利益</th><th>利益率</th><th>買取価格</th><th>ヤフショ</th><th>ポイント</th>
<th>一致</th><th>買取商品</th><th>ヤフショ商品</th><th>注意</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table></div>
<p class="sub">Web Services by Yahoo! JAPAN</p>
</body></html>
"""
