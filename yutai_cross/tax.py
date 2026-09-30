"""実績記録（executed.csv）から、確定申告用の年間メモを作る。

一般的な取扱い:
- 株主優待は「雑所得（その他）」。収入金額は受け取った時点の時価（金券は額面など）。
- 貸株料・逆日歩・信用取引の手数料・配当落調整金は、信用取引の譲渡損益の計算に含まれ、
  特定口座なら年間取引報告書に反映済み。優待の雑所得の経費として二重に差し引かない。
判断に迷うもの（割引券の評価など）は税務署か税理士に確認すること。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from .watchlist import _read_text, _to_number

HEADER_ALIASES: Dict[str, str] = {
    "受取日": "received", "received": "received",
    "コード": "code", "code": "code",
    "銘柄名": "name", "name": "name",
    "優待内容": "benefit", "benefit": "benefit",
    "評価額": "value", "value": "value",
    "売却額": "sold", "sold": "sold",
    "貸株料": "lending", "lending": "lending",
    "逆日歩": "gyakuhibu", "gyakuhibu": "gyakuhibu",
    "手数料": "commission", "commission": "commission",
    "受取配当(税引前)": "dividend", "受取配当": "dividend", "dividend": "dividend",
    "配当落調整金": "adjustment", "adjustment": "adjustment",
    "メモ": "memo", "memo": "memo",
}


@dataclass
class Executed:
    received: date
    code: str
    name: str
    benefit: str
    value: int
    sold: Optional[int]
    lending: int
    gyakuhibu: int
    commission: int
    dividend: int
    adjustment: int
    memo: str

    @property
    def income(self) -> int:
        """雑所得の収入金額として計上する額（評価額。未入力なら売却額）。"""
        if self.value:
            return self.value
        return self.sold or 0

    @property
    def cross_cost(self) -> int:
        return self.lending + self.gyakuhibu + self.commission


def parse_executed(text: str, source: str = "executed") -> List[Executed]:
    rows = [r for r in csv.reader(io.StringIO(text))
            if any(c.strip() for c in r) and not r[0].lstrip().startswith("#")]
    if not rows:
        return []
    header = [HEADER_ALIASES.get(h.strip(), h.strip()) for h in rows[0]]
    for key in ("received", "code"):
        if key not in header:
            raise ValueError(f"{source}: 必須の列がありません: {key}")
    out = []
    for i, row in enumerate(rows[1:], start=2):
        rec = {header[j]: (row[j].strip() if j < len(row) else "") for j in range(len(header))}

        def num(key: str) -> int:
            v = _to_number(rec.get(key, ""))
            return int(round(v)) if v is not None else 0

        try:
            sold = _to_number(rec.get("sold", ""))
            out.append(Executed(
                received=date.fromisoformat(rec["received"].replace("/", "-")),
                code=rec["code"], name=rec.get("name", ""), benefit=rec.get("benefit", ""),
                value=num("value"), sold=int(round(sold)) if sold is not None else None,
                lending=num("lending"), gyakuhibu=num("gyakuhibu"), commission=num("commission"),
                dividend=num("dividend"), adjustment=num("adjustment"), memo=rec.get("memo", ""),
            ))
        except ValueError as e:
            raise ValueError(f"{source} {i}行目: {e}") from None
    return out


def load_executed(path: Path) -> List[Executed]:
    return parse_executed(_read_text(Path(path)), source=str(path))


def tax_memo(records: List[Executed], year: int) -> str:
    rows = sorted((r for r in records if r.received.year == year), key=lambda r: r.received)
    income = sum(r.income for r in rows)
    cross_cost = sum(r.cross_cost for r in rows)
    dividend = sum(r.dividend for r in rows)
    adjustment = sum(r.adjustment for r in rows)
    sold_total = sum(r.sold or 0 for r in rows)

    lines = [
        f"# {year}年分 優待クロス 確定申告メモ",
        "",
        "| 受取日 | コード | 銘柄 | 優待内容 | 評価額(収入) | 売却額 | クロス費用 |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r.received.isoformat()} | {r.code} | {r.name} | {r.benefit} | {r.income:,} | "
            f"{'' if r.sold is None else f'{r.sold:,}'} | {r.cross_cost:,} |"
        )
    lines += [
        "",
        "## 集計",
        "",
        f"- 優待の件数: {len(rows)}件",
        f"- **雑所得（その他）の収入金額: {income:,}円** ← 確定申告書の「雑所得・その他」に記入",
        "- 必要経費: 0円（クロス費用は下記のとおり株式の譲渡損益側で処理済みのため）",
        f"- 参考）クロス費用（貸株料・逆日歩・手数料）合計: {cross_cost:,}円"
        " → 特定口座の年間取引報告書の譲渡損益に反映済み",
        f"- 参考）受取配当(税引前) {dividend:,}円 ／ 支払った配当落調整金 {adjustment:,}円"
        " → 特定口座(源泉徴収あり)＋株式数比例配分方式なら口座内で自動的に損益通算",
    ]
    if sold_total:
        lines.append(f"- 参考）優待品・優待券の売却額合計: {sold_total:,}円"
                     "（評価額と大きく違う場合の扱いは税理士に確認）")
    lines += [
        "",
        "## メモ",
        "",
        "- 「給与以外の所得が20万円以下なら申告不要」は、確定申告をしない会社員向けの特例。"
        "せどり等で確定申告をするなら、優待の雑所得も金額にかかわらず含める。住民税にはこの特例がない。",
        "- 優待品・優待券は、せどりの仕入・売上とは混ぜずに別管理する（事業の帳簿には載せない）。",
        "- 事業用の口座から証券口座に入金した場合は、会計ソフトでは「事業主貸」で処理する。",
        "- この集計は目安です。最終判断は税務署・税理士に確認してください。",
        "",
    ]
    return "\n".join(lines)
