"""利益の計算と、買取商品ごとのヤフショ最良候補の判定。

利益 = 買取価格 − 仕入価格 − 仕入送料 − 発送送料 − 振込手数料 − その他経費 − 消費税の増加分
       ＋ ポイント（円換算）
「現金利益」はポイントを含めない額。所得税・住民税は含めない（税引前）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .buyback import BuybackItem
from .config import TAX_EXEMPT, TAX_GENERAL, TAX_SIMPLIFIED, TAX_SPECIAL, Settings
from .matching import (
    clean_query,
    color_caution,
    color_conflict,
    contains_any,
    has_model_number,
    kit_mismatch,
    name_matches,
)
from .yahoo import Offer

TAX_RATE = 0.10  # カメラ・レンズは標準税率

JUDGE_GOOD = "◎黒字"
JUDGE_THIN = "△薄利"
JUDGE_LOSS = "×赤字"
JUDGE_NONE = "－なし"
JUDGE_ORDER = {JUDGE_GOOD: 0, JUDGE_THIN: 1, JUDGE_LOSS: 2, JUDGE_NONE: 3}

MATCH_JAN = "JAN"
MATCH_NAME = "名前(要確認)"


@dataclass
class Breakdown:
    sell: int                 # 買取価格
    buy: int                  # 仕入価格（税込）
    shipping_in: int = 0      # 仕入れの送料
    shipping_out: int = 0     # 買取サイトへの送料
    transfer_fee: int = 0
    other: int = 0
    points: int = 0           # もらえるポイント数
    point_yen: int = 0        # ポイントの円換算
    consumption_tax: int = 0  # この取引で増える消費税の納付額（目安）

    @property
    def cost(self) -> int:
        return self.buy + self.shipping_in + self.shipping_out + self.transfer_fee + self.other

    @property
    def cash_profit(self) -> int:
        return self.sell - self.cost - self.consumption_tax

    @property
    def profit(self) -> int:
        return self.cash_profit + self.point_yen

    @property
    def outlay(self) -> int:
        """仕入れに必要な現金（利益率の分母）。"""
        return self.buy + self.shipping_in


def consumption_tax(sell: int, taxable_costs: int, s: Settings) -> int:
    """この取引で増える消費税の納付額の目安（円）。"""
    out_tax = sell * TAX_RATE / (1 + TAX_RATE)
    if s.tax_mode == TAX_EXEMPT:
        return 0
    if s.tax_mode == TAX_GENERAL:
        in_tax = taxable_costs * TAX_RATE / (1 + TAX_RATE) * s.input_credit_ratio
        return round(out_tax - in_tax)
    if s.tax_mode == TAX_SIMPLIFIED:
        return round(out_tax * (1 - s.deemed_purchase_rate))
    if s.tax_mode == TAX_SPECIAL:
        return round(out_tax * s.special_ratio)
    raise ValueError(f"tax_mode が不正です: {s.tax_mode}")


def calc(sell: int, buy: int, s: Settings, *, api_points: int = 0, shipping_in: int = 0) -> Breakdown:
    # ヤフショのポイントは税抜価格に対して付く
    extra = int(buy * 100 // 110 * s.extra_point_rate)
    points = api_points + extra
    b = Breakdown(sell=sell, buy=buy, shipping_in=shipping_in, shipping_out=s.send_shipping_yen,
                  transfer_fee=s.transfer_fee_yen, other=s.other_cost_yen, points=points,
                  point_yen=int(points * s.point_value))
    b.consumption_tax = consumption_tax(sell, b.cost, s)
    return b


def judge(b: Breakdown, s: Settings) -> str:
    p = b.profit if s.judge_with_points else b.cash_profit
    if p >= s.min_profit_yen and b.outlay > 0 and p / b.outlay >= s.min_profit_rate:
        return JUDGE_GOOD
    if p > 0:
        return JUDGE_THIN
    return JUDGE_LOSS


@dataclass
class Result:
    item: BuybackItem
    offer: Optional[Offer] = None
    breakdown: Optional[Breakdown] = None
    judge: str = JUDGE_NONE
    match: str = ""
    cautions: List[str] = field(default_factory=list)
    candidates: int = 0       # 条件に合ったヤフショの商品数
    excluded: int = 0         # 除外した商品数
    note: str = ""            # 見つからなかった理由・エラー

    @property
    def sort_key(self):
        p = self.breakdown.profit if self.breakdown else -10**12
        return (JUDGE_ORDER[self.judge], -p)


def offer_cautions(item: BuybackItem, offer: Offer, b: Breakdown, s: Settings, by_jan: bool) -> List[str]:
    out = []
    words = contains_any(offer.name, s.caution_words)
    if words:
        out.append("要確認の語: " + "・".join(words))
    if offer.shipping_free is not True:
        out.append(f"送料{'不明' if offer.shipping_free is None else '別'}（{s.assumed_shipping_yen:,}円で計算）")
    if offer.store_reviews is not None and offer.store_reviews < s.min_store_reviews:
        out.append(f"ストア評価の件数が少ない（{offer.store_reviews}件）")
    elif offer.store_rating is not None and offer.store_rating < s.min_store_rating:
        out.append(f"ストア評価が低い（{offer.store_rating:.2f}）")
    if by_jan and offer.price < item.price * s.price_floor_ratio:
        out.append("相場より極端に安い（価格誤り・詐欺ストアに注意）")
    if not by_jan:
        c = color_caution(item.name, offer.name)
        if c:
            out.append(c)
    if b.cash_profit <= 0 < b.profit:
        out.append("ポイント頼み（現金だけだと赤字）")
    return out


def evaluate(item: BuybackItem, offers: List[Offer], s: Settings, by_jan: bool) -> Result:
    """候補の中から利益が最大のものを選ぶ。"""
    res = Result(item=item, match=MATCH_JAN if by_jan else MATCH_NAME)
    best_key = None
    for o in offers:
        if contains_any(o.name, s.exclude_words):
            res.excluded += 1
            continue
        if not by_jan:
            if (not name_matches(item.name, o.name) or kit_mismatch(item.name, o.name)
                    or color_conflict(item.name, o.name) or o.price < item.price * s.price_floor_ratio):
                res.excluded += 1
                continue
        ship = 0 if o.shipping_free else s.assumed_shipping_yen
        b = calc(item.price, o.price, s, api_points=o.points, shipping_in=ship)
        res.candidates += 1
        key = b.profit if s.judge_with_points else b.cash_profit
        if best_key is None or key > best_key:
            best_key = key
            res.offer, res.breakdown = o, b
    if res.offer is not None and res.breakdown is not None:
        res.judge = judge(res.breakdown, s)
        res.cautions = offer_cautions(item, res.offer, res.breakdown, s, by_jan)
    return res


Searcher = Callable[..., List[Offer]]


def check_item(item: BuybackItem, s: Settings, search: Searcher) -> Result:
    """JANで探し、見つからなければ商品名で探す。search は yahoo.search と同じ引数を受け取る関数。"""
    tried_jan = False
    if item.jan:
        tried_jan = True
        res = evaluate(item, search(jan=item.jan), s, by_jan=True)
        if res.offer is not None:
            return res
    if not has_model_number(item.name):
        return Result(item=item, note="JANで見つからず、商品名に型番がないため名前検索を省略" if tried_jan
                      else "JANがなく、商品名に型番がないため検索を省略（誤判定を防ぐため）")
    res = evaluate(item, search(query=clean_query(item.name), price_from=int(item.price * s.price_floor_ratio)),
                   s, by_jan=False)
    if res.offer is None:
        res.note = "ヤフショで同じ商品が見つからず" + (f"（{res.excluded}件は除外）" if res.excluded else "")
    return res
