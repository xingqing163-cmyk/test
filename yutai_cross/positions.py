"""建玉の記録（mydata/positions.csv）。

クロスしたら add、現渡ししたら close、優待が届いたら received で記録する。
記録があると、today がその建玉の現渡し日・返済期日・優待の到着確認を知らせる。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, List, Optional

from .config import normalize_code
from .tax import parse_date
from .watchlist import csv_rows, nfkc, read_text, to_number

STATUS_OPEN = "保有中"
STATUS_CLOSED = "現渡し済"
STATUS_RECEIVED = "受取済"
STATUSES = (STATUS_OPEN, STATUS_CLOSED, STATUS_RECEIVED)

HEADERS = ["ID", "コード", "銘柄名", "権利確定日", "方法", "建日", "株数", "約定単価",
           "状態", "現渡し日", "優待受取日", "メモ"]


@dataclass
class Position:
    id: str
    code: str
    name: str
    record_date: date
    method_id: str
    entry_date: date
    shares: int
    price: float
    status: str = STATUS_OPEN
    close_date: Optional[date] = None
    received_date: Optional[date] = None
    memo: str = ""

    @property
    def notional(self) -> float:
        return self.price * self.shares

    @property
    def label(self) -> str:
        return f"#{self.id} {self.code} {self.name}"


def _opt_date(text: str) -> Optional[date]:
    return parse_date(text) if nfkc(text) else None


def parse_positions(text: str, source: str = "positions") -> List[Position]:
    rows = csv_rows(text)
    if not rows:
        return []
    header = [nfkc(h) for h in rows[0][1]]
    missing = [h for h in HEADERS[:8] if h not in header]
    if missing:
        raise ValueError(f"{source}: 必須の列がありません: {'、'.join(missing)}")
    out: List[Position] = []
    for line_no, row in rows[1:]:
        rec = {header[j]: (row[j].strip() if j < len(row) else "") for j in range(len(header))}
        try:
            status = rec.get("状態") or STATUS_OPEN
            if status not in STATUSES:
                raise ValueError(f"状態は {'・'.join(STATUSES)} のどれかにしてください: {status!r}")
            out.append(Position(
                id=rec["ID"], code=normalize_code(rec["コード"]), name=rec.get("銘柄名", ""),
                record_date=parse_date(rec["権利確定日"]), method_id=rec["方法"],
                entry_date=parse_date(rec["建日"]), shares=int(to_number(rec["株数"]) or 0),
                price=to_number(rec["約定単価"]) or 0.0, status=status,
                close_date=_opt_date(rec.get("現渡し日", "")),
                received_date=_opt_date(rec.get("優待受取日", "")), memo=rec.get("メモ", ""),
            ))
        except (ValueError, KeyError) as e:
            raise ValueError(f"{source} の {line_no}行目（Excelの行番号）: {e}") from None
    return out


def load_positions(path: Path) -> List[Position]:
    path = Path(path)
    if not path.exists():
        return []
    return parse_positions(read_text(path), source=str(path))


def render_positions(positions: Iterable[Position]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(HEADERS)
    for p in positions:
        w.writerow([
            p.id, p.code, p.name, p.record_date.isoformat(), p.method_id, p.entry_date.isoformat(),
            p.shares, f"{p.price:g}", p.status,
            p.close_date.isoformat() if p.close_date else "",
            p.received_date.isoformat() if p.received_date else "", p.memo,
        ])
    return buf.getvalue()


def save_positions(path: Path, positions: Iterable[Position]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_positions(positions), encoding="utf-8-sig")


def next_id(positions: List[Position]) -> str:
    nums = [int(p.id) for p in positions if p.id.isdigit()]
    return str(max(nums, default=0) + 1)


def find_position(positions: List[Position], key: str, statuses: Iterable[str]) -> Position:
    """ID かコードで建玉を1つ選ぶ。候補が複数ならエラー（ID で指定してもらう）。"""
    statuses = tuple(statuses)
    key_n = normalize_code(key).lstrip("#")
    by_id = [p for p in positions if p.id == key_n and p.status in statuses]
    if by_id:
        return by_id[0]
    by_code = [p for p in positions if p.code == key_n and p.status in statuses]
    if not by_code:
        raise ValueError(f"{key} に当たる建玉（状態: {'・'.join(statuses)}）が見つかりません。"
                         "`position list` で確認してください")
    if len(by_code) > 1:
        ids = "、".join(f"#{p.id}（権利確定日 {p.record_date.isoformat()}）" for p in by_code)
        raise ValueError(f"{key} の建玉が複数あります: {ids}。ID で指定してください（例: position close {by_code[0].id}）")
    return by_code[0]
