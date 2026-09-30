import unittest
from datetime import date

from yutai_cross.config import Settings
from yutai_cross.jpx_calendar import MarketCalendar
from yutai_cross.planner import (
    actions_on,
    build_plan,
    methods_for,
    peak_capital,
    upcoming_rights,
)
from yutai_cross.watchlist import WatchItem


def item(**kw):
    base = dict(code="X001", name="テスト", record_months=[10], price=1850, benefit_value=3000,
                ippan_brokers=["sbi_short"], popularity="低")
    base.update(kw)
    return WatchItem(**base)


class MethodsForTest(unittest.TestCase):
    def ids(self, **kw):
        return [m.id for m in methods_for(item(**kw), Settings())]

    def test_aliases(self):
        self.assertEqual(self.ids(ippan_brokers=["SBI"]), ["sbi_short", "sbi_long"])
        self.assertEqual(self.ids(ippan_brokers=["楽天証券"]), ["rakuten_short", "rakuten_long"])
        self.assertEqual(self.ids(ippan_brokers=["SBI短期", "楽天無期限"]), ["sbi_short", "rakuten_long"])
        self.assertEqual(self.ids(ippan_brokers=None),
                         ["sbi_short", "sbi_long", "rakuten_short", "rakuten_long"])
        self.assertEqual(self.ids(ippan_brokers=[], taishaku=True), ["seido"])
        self.assertEqual(self.ids(ippan_brokers=[]), [])


class UpcomingRightsTest(unittest.TestCase):
    def test_rolls_to_next_year(self):
        cal = MarketCalendar()
        rds = upcoming_rights(item(record_months=[3, 9]), date(2026, 9, 30), cal)
        self.assertEqual([r.record_date for r in rds], [date(2027, 3, 31), date(2027, 9, 30)])
        # 権利落ち日当日はまだ対象（現渡しの日）
        rds = upcoming_rights(item(record_months=[9]), date(2026, 9, 29), cal)
        self.assertEqual(rds[0].record_date, date(2026, 9, 30))


class PlanTest(unittest.TestCase):
    today = date(2026, 10, 1)

    def plan(self, items, settings=None, today=None):
        return build_plan(items, today or self.today, settings or Settings())

    def test_low_popularity_enters_on_last_day(self):
        p = self.plan([item()]).plans[0]
        self.assertEqual(p.rights.last_cum_date, date(2026, 10, 28))
        self.assertEqual(p.best.method.id, "sbi_short")
        self.assertEqual(p.recommended.entry_date, date(2026, 10, 28))
        self.assertEqual(p.best.window_start, date(2026, 10, 7))  # SBI短期初日
        self.assertEqual(p.phase, "待機（あと18営業日）")

    def test_high_popularity_enters_early(self):
        p = self.plan([item(popularity="高")]).plans[0]
        self.assertEqual(p.recommended.entry_date, date(2026, 10, 7))
        self.assertGreaterEqual(p.recommended.net_expected, 0.5 * p.best.cheapest.net_expected)

    def test_thin_profit_limits_early_entry(self):
        # 優待が小さいと、先回りできる日が短期初日より後ろになる
        p = self.plan([item(popularity="高", benefit_value=700)]).plans[0]
        self.assertGreater(p.recommended.entry_date, p.best.window_start)
        self.assertGreaterEqual(p.recommended.net_expected, 300)

    def test_skip_when_unprofitable(self):
        r = self.plan([item(benefit_value=300, dividend=40)])
        self.assertEqual(r.recommended, [])
        self.assertEqual(r.passed[0].phase, "見送り")

    def test_long_term_condition_skipped_by_default(self):
        it = item(long_term="1年以上継続保有")
        self.assertIsNone(self.plan([it]).plans[0].best)
        s = Settings(include_long_term=True)
        self.assertIsNotNone(self.plan([it], s).plans[0].best)

    def test_seido_rejected_on_worst_case_but_ok_on_expected(self):
        it = item(code="X004", record_months=[11], record_day="20", price=980, benefit_value=1500,
                  ippan_brokers=[], taishaku=True, gyakuhibu_est=2)
        self.assertIsNone(self.plan([it]).plans[0].best)
        s = Settings(seido_risk_basis="expected")
        p = self.plan([it], s).plans[0]
        self.assertEqual(p.best.method.id, "seido")
        self.assertTrue(any("逆日歩" in w for w in p.warnings))

    def test_excluded_and_missing_price(self):
        s = Settings(exclude_codes=["X001"])
        r = self.plan([item(), item(code="X002", price=None)], s)
        self.assertEqual(r.plans, [])
        self.assertEqual(len(r.skipped), 2)

    def test_price_override(self):
        r = build_plan([item(price=None)], self.today, Settings(), prices={"X001": 1850})
        self.assertEqual(len(r.plans), 1)

    def test_horizon(self):
        self.assertEqual(self.plan([item(record_months=[12])]).plans, [])
        s = Settings(horizon_days=120)
        self.assertEqual(len(self.plan([item(record_months=[12])], s).plans), 1)

    def test_ranking_order(self):
        r = self.plan([item(code="A", benefit_value=1000), item(code="B", benefit_value=5000)])
        self.assertEqual([p.item.code for p in r.plans], ["B", "A"])


class ActionsTest(unittest.TestCase):
    def setUp(self):
        self.items = [item(), item(code="X002", ippan_brokers=["SBI短期", "楽天短期"], popularity="中")]

    def kinds(self, day, today):
        r = build_plan(self.items, today, Settings())
        return [(a.kind, a.plan.item.code) for a in actions_on(day, r)]

    def test_short_open_day(self):
        self.assertIn(("短期初日", "X001"), self.kinds(date(2026, 10, 7), date(2026, 10, 6)))

    def test_last_day_and_ex_date(self):
        self.assertIn(("最終日", "X001"), self.kinds(date(2026, 10, 28), date(2026, 10, 28)))
        self.assertIn(("現渡し", "X001"), self.kinds(date(2026, 10, 29), date(2026, 10, 29)))

    def test_after_last_day_goes_to_settling(self):
        r = build_plan(self.items, date(2026, 10, 29), Settings())
        self.assertEqual(r.recommended, [])
        self.assertEqual(r.passed, [])
        self.assertEqual({p.item.code for p in r.settling}, {"X001", "X002"})
        self.assertEqual(r.settling[0].phase, "本日現渡し")

    def test_entry_period_shown_for_next_day(self):
        # X002（人気 中）は 10/16 にはエントリー目安日に入っている
        r = build_plan(self.items, date(2026, 10, 16), Settings())
        x002 = next(p for p in r.plans if p.item.code == "X002")
        self.assertEqual(x002.recommended.entry_date, date(2026, 10, 16))
        acts = [a for a in actions_on(date(2026, 10, 19), r) if a.kind == "エントリー"]
        self.assertEqual([a.plan.item.code for a in acts], ["X002"])
        self.assertIn("エントリー期間中", acts[0].text)


class PeakCapitalTest(unittest.TestCase):
    def test_overlapping_positions(self):
        items = [item(code="A"), item(code="B", record_months=[11])]
        r = build_plan(items, date(2026, 10, 1), Settings(horizon_days=90))
        peak, day = peak_capital(r.plans)
        cap = r.plans[0].capital
        self.assertEqual(peak, cap)  # 10月分と11月分は期間が重ならない
        self.assertIsNotNone(day)


if __name__ == "__main__":
    unittest.main()
