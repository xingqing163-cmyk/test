import unittest
from datetime import date

from yutai_cross.config import Settings, ShortMethod
from yutai_cross.costs import (
    approx_max_gyakuhibu,
    dividend_cost,
    lending_days,
    lending_fee,
    quote,
)
from yutai_cross.jpx_calendar import MarketCalendar
from yutai_cross.watchlist import WatchItem


def methods():
    return {m.id: m for m in Settings().methods}


class PrimitiveTest(unittest.TestCase):
    def test_lending_days(self):
        # 1晩のクロス: 受渡日が翌営業日 → 両端入れで2日分
        self.assertEqual(lending_days(date(2026, 9, 30), date(2026, 10, 1), "both"), 2)
        self.assertEqual(lending_days(date(2026, 9, 30), date(2026, 10, 1), "one"), 1)
        # 金曜→月曜
        self.assertEqual(lending_days(date(2026, 10, 30), date(2026, 11, 2), "both"), 4)

    def test_lending_fee_floor(self):
        self.assertEqual(lending_fee(200_000, 0.039, 2), 42)  # 42.7 → 42

    def test_dividend_cost_ippan(self):
        # 一般信用は配当の100%を払う。損益通算で税が戻れば実質0
        self.assertEqual(dividend_cost(30, 100, 1.0, True), 0)
        # 戻らない場合は源泉税 20.315% 分が損
        self.assertEqual(dividend_cost(30, 100, 1.0, False), 609)

    def test_dividend_cost_seido(self):
        # 制度信用は84.685%を払う。損益通算できれば少し得（マイナスのコスト）
        self.assertEqual(dividend_cost(30, 100, 0.84685, True), -366)
        # 損益通算できなければ 5% 分が損
        self.assertEqual(dividend_cost(30, 100, 0.84685, False), 150)

    def test_approx_max_gyakuhibu(self):
        self.assertEqual(approx_max_gyakuhibu(980), 2.0)
        self.assertEqual(approx_max_gyakuhibu(1000), 2.0)
        self.assertEqual(approx_max_gyakuhibu(1001), 3.0)
        self.assertEqual(approx_max_gyakuhibu(4500), 10.0)
        self.assertEqual(approx_max_gyakuhibu(600_000), 1200.0)


class QuoteTest(unittest.TestCase):
    def setUp(self):
        self.cal = MarketCalendar()
        self.settings = Settings()
        self.rd = self.cal.rights_dates(date(2026, 10, 31))

    def item(self, **kw):
        base = dict(code="X", name="x", record_months=[10], price=1850, benefit_value=3000,
                    dividend=15, taishaku=True)
        base.update(kw)
        return WatchItem(**base)

    def test_ippan_last_day(self):
        q = quote(self.item(), self.rd, methods()["sbi_short"], self.rd.last_cum_date,
                  self.cal, self.settings)
        self.assertEqual(q.open_settle, date(2026, 10, 30))
        self.assertEqual(q.close_settle, date(2026, 11, 2))
        self.assertEqual(q.lending_days, 4)
        self.assertEqual(q.lending_fee, 79)
        self.assertEqual(q.dividend_cost, 0)
        self.assertEqual(q.net_expected, 3000 - 79)
        self.assertEqual(q.net_worst, q.net_expected)

    def test_ippan_early_costs_more(self):
        early = quote(self.item(), self.rd, methods()["sbi_short"], date(2026, 10, 7),
                      self.cal, self.settings)
        self.assertEqual(early.lending_days, 25)  # 10/9〜11/2（両端入れ）
        self.assertEqual(early.lending_fee, 494)

    def test_seido_worst_case(self):
        rd = self.cal.rights_dates(date(2026, 11, 20))
        it = self.item(price=980, benefit_value=1500, dividend=0, gyakuhibu_est=2)
        q = quote(it, rd, methods()["seido"], rd.last_cum_date, self.cal, self.settings)
        self.assertEqual(q.gyakuhibu_days, 4)          # 11/20(金)→11/24(火)、11/23は祝日
        self.assertEqual(q.gyakuhibu_expected, 800)    # 2円×100株×4日
        self.assertEqual(q.gyakuhibu_worst, 3200)      # 2.0円×4倍×100株×4日
        self.assertEqual(q.decision_net(self.settings), q.net_worst)
        self.settings.seido_risk_basis = "expected"
        self.assertEqual(q.decision_net(self.settings), q.net_expected)

    def test_resale_valuation_and_tax(self):
        self.settings.valuation = "resale"
        self.settings.benefit_tax_rate = 0.3
        q = quote(self.item(resale_value=1800), self.rd, methods()["sbi_short"],
                  self.rd.last_cum_date, self.cal, self.settings)
        self.assertEqual(q.value, 1800)
        self.assertEqual(q.benefit_tax, 540)
        self.assertEqual(q.net_expected, 1800 - 540 - 79)

    def test_entry_after_last_cum_rejected(self):
        with self.assertRaises(ValueError):
            quote(self.item(), self.rd, methods()["sbi_short"], self.rd.ex_date,
                  self.cal, self.settings)

    def test_commission(self):
        m = ShortMethod(id="x", broker="X", label="x", lending_rate=0.0,
                        commission_buy=100, commission_short=50)
        q = quote(self.item(dividend=0), self.rd, m, self.rd.last_cum_date, self.cal, self.settings)
        self.assertEqual(q.cost_expected, 150)


if __name__ == "__main__":
    unittest.main()
