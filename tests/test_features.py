"""追加機能（建玉の記録・資金枠・入力チェック・カレンダーなど）のテスト。"""

import contextlib
import io
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from yutai_cross.cli import main
from yutai_cross.config import Settings
from yutai_cross.jpx_calendar import MarketCalendar
from yutai_cross.planner import (
    actions_on,
    build_plan,
    capital_usage,
    peak_capital,
    position_alerts,
)
from yutai_cross.positions import (
    STATUS_CLOSED,
    STATUS_OPEN,
    Position,
    find_position,
    next_id,
    parse_positions,
    render_positions,
)
from yutai_cross.report import render_ics, render_markdown
from yutai_cross.watchlist import WatchItem

ROOT = Path(__file__).resolve().parent.parent


def item(**kw):
    base = dict(code="A001", name="テスト", record_months=[10], price=1850, benefit_value=3000,
                ippan_brokers=["SBI短期", "楽天短期"], popularity="中")
    base.update(kw)
    return WatchItem(**base)


def pos(**kw):
    base = dict(id="1", code="A001", name="テスト", record_date=date(2026, 10, 31), method_id="rakuten_short",
                entry_date=date(2026, 10, 16), shares=100, price=1850.0)
    base.update(kw)
    return Position(**base)


class PositionsFileTest(unittest.TestCase):
    def test_roundtrip(self):
        ps = [pos(), pos(id="2", code="B002", status=STATUS_CLOSED, close_date=date(2026, 10, 29))]
        back = parse_positions(render_positions(ps))
        self.assertEqual(back, ps)
        self.assertEqual(next_id(back), "3")

    def test_find_by_code_or_id(self):
        ps = [pos(), pos(id="2", record_date=date(2027, 4, 30))]
        self.assertEqual(find_position(ps, "#2", [STATUS_OPEN]).id, "2")
        with self.assertRaisesRegex(ValueError, "複数"):
            find_position(ps, "A001", [STATUS_OPEN])
        with self.assertRaisesRegex(ValueError, "見つかりません"):
            find_position(ps, "Z999", [STATUS_OPEN])


class PositionPlanningTest(unittest.TestCase):
    def test_position_suppresses_entry_and_drives_genwatashi(self):
        r = build_plan([item()], date(2026, 10, 20), Settings(), positions=[pos()])
        self.assertTrue(r.plans[0].crossed)
        self.assertEqual(actions_on(date(2026, 10, 20), r), [])
        acts = actions_on(date(2026, 10, 29), build_plan([item()], date(2026, 10, 29), Settings(),
                                                         positions=[pos()]))
        self.assertEqual([a.kind for a in acts], ["現渡し"])
        self.assertIn("返済期日 2026-10-29(木)", acts[0].text)
        self.assertIn("position close 1", acts[0].text)

    def test_no_genwatashi_noise_for_passed_items(self):
        poor = item(code="P001", benefit_value=100)
        # 記録を使っていない場合: 候補になり得た銘柄だけ
        r = build_plan([item(), poor], date(2026, 10, 29), Settings())
        self.assertEqual([a.code for a in actions_on(date(2026, 10, 29), r)], ["A001"])
        # 記録を使っている場合: 記録のある建玉だけ
        other = pos(id="9", code="C003", record_date=date(2026, 11, 30), entry_date=date(2026, 11, 16))
        r = build_plan([item(), poor], date(2026, 10, 29), Settings(), positions=[other])
        self.assertEqual(actions_on(date(2026, 10, 29), r), [])

    def test_alerts(self):
        s = Settings()
        # 返済期日を過ぎても保有中
        r = build_plan([], date(2026, 10, 30), s, positions=[pos()])
        self.assertTrue(any("返済期日" in a for a in position_alerts(r)))
        # 現渡し済みで60日たった → 優待の確認
        closed = pos(status=STATUS_CLOSED, close_date=date(2026, 10, 29))
        r = build_plan([], date(2027, 1, 5), s, positions=[closed])
        self.assertTrue(any("position received 1" in a for a in position_alerts(r)))
        r = build_plan([], date(2026, 11, 20), s, positions=[closed])
        self.assertEqual(position_alerts(r), [])

    def test_ics_has_position_events(self):
        r = build_plan([item()], date(2026, 10, 20), Settings(), positions=[pos(method_id="sbi_short",
                                                                                 entry_date=date(2026, 10, 7))])
        ics = render_ics(r, Settings(), now=datetime(2026, 10, 20, tzinfo=timezone.utc))
        self.assertIn("UID:pos-1-ex@yutai-cross", ics)
        self.assertNotIn("エントリー目安", ics)


class BudgetTest(unittest.TestCase):
    def test_capital_usage_has_minimum_deposit(self):
        s = Settings()
        self.assertEqual(capital_usage([185_000], s), 485_000)
        self.assertEqual(capital_usage([2_000_000], s), 2_600_000)
        self.assertEqual(capital_usage([], s), 0)

    def test_budget_excludes_lower_yield_overlap(self):
        items = [item(code="HIGH", benefit_value=5000), item(code="LOW", benefit_value=1000, price=3000)]
        s = Settings(capital_budget_yen=600_000)
        r = build_plan(items, date(2026, 10, 1), s)
        self.assertEqual([p.item.code for p in r.recommended], ["HIGH"])
        low = next(p for p in r.plans if p.item.code == "LOW")
        self.assertIn("資金枠", low.excluded_reason)
        self.assertLessEqual(peak_capital(r)[0], 600_000)
        # 資金枠で見送った銘柄には、権利付最終日にも発注の通知を出さない
        last_day = [(a.kind, a.code) for a in actions_on(date(2026, 10, 28), r)]
        self.assertIn(("最終日", "HIGH"), last_day)
        self.assertNotIn(("最終日", "LOW"), last_day)

    def test_open_position_uses_budget(self):
        s = Settings(capital_budget_yen=500_000)
        held = pos(code="HELD", record_date=date(2026, 10, 31), entry_date=date(2026, 10, 1), price=1850)
        r = build_plan([item()], date(2026, 10, 1), s, positions=[held])
        self.assertEqual(r.recommended, [])


class AutoPopularityTest(unittest.TestCase):
    def test_inferred_when_blank(self):
        s = Settings()
        march = build_plan([item(record_months=[3], popularity="")], date(2027, 2, 15), s).plans[0]
        self.assertEqual((march.popularity, march.popularity_auto), ("高", True))
        high_yield = build_plan([item(popularity="", price=1000)], date(2026, 10, 1), s).plans[0]
        self.assertEqual(high_yield.popularity, "高")  # 3000/(1000*100)=3%
        plain = build_plan([item(popularity="", price=5000, benefit_value=1000)], date(2026, 10, 1), s).plans[0]
        self.assertEqual(plain.popularity, "中")
        off = build_plan([item(popularity="")], date(2026, 10, 1), Settings(auto_popularity=False)).plans[0]
        self.assertEqual((off.popularity, off.popularity_auto), ("", False))


class RatesWarningTest(unittest.TestCase):
    def test_stale_rates(self):
        s = Settings(rates_checked_on=date(2026, 9, 30))
        self.assertIsNone(s.rates_warning(date(2026, 12, 1)))
        self.assertIn("97日", s.rates_warning(date(2027, 1, 5)))
        self.assertIn("未入力", Settings(rates_checked_on=None).rates_warning(date(2026, 10, 1)))


class ReportLinksTest(unittest.TestCase):
    def test_links_only_for_tse_codes(self):
        cal = MarketCalendar()
        r = build_plan([item(code="7203"), item(code="X001")], date(2026, 10, 1), Settings(), cal=cal)
        md = render_markdown(r, Settings(), cal)
        self.assertIn("https://kabutan.jp/stock/?code=7203", md)
        self.assertNotIn("code=X001", md)


class CliFeatureTest(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        code = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(argv)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                err.write(str(e.code))
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        self.run_main(["init"])

    def tearDown(self):
        os.chdir(self.cwd)

    def test_position_lifecycle(self):
        code, out, _ = self.run_main(["position", "add", "X001", "--method", "rakuten_short",
                                      "--date", "2026-10-16", "--price", "1850"])
        self.assertEqual(code, 0, out)
        self.assertIn("返済期日: 2026-10-29(木)", out)
        # 同じ権利日の二重記録は止める
        code, _, err = self.run_main(["position", "add", "X001", "--method", "rakuten_short",
                                      "--date", "2026-10-16", "--price", "1850"])
        self.assertNotEqual(code, 0)
        self.assertIn("すでにあります", err)
        # 権利付最終日以前の現渡しは止める
        code, _, err = self.run_main(["position", "close", "1", "--date", "2026-10-28"])
        self.assertNotEqual(code, 0)
        self.assertIn("権利がなくなります", err)
        code, _, _ = self.run_main(["position", "close", "1", "--date", "2026-10-29"])
        self.assertEqual(code, 0)
        code, out, _ = self.run_main(["position", "received", "X001", "--value", "3000",
                                      "--date", "2027-01-08"])
        self.assertEqual(code, 0, out)
        executed = Path("mydata/executed.csv").read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(len(executed), 2)
        self.assertTrue(executed[1].startswith("2027-01-08,X001,"))
        code, out, _ = self.run_main(["tax", "--year", "2027"])
        self.assertIn("3,000円", out)
        code, out, _ = self.run_main(["position", "list", "--all"])
        self.assertIn("受取済", out)

    def test_position_add_validations(self):
        code, _, err = self.run_main(["position", "add", "X001", "--method", "rakuten_short",
                                      "--date", "2026-10-07", "--price", "1850"])
        self.assertNotEqual(code, 0)
        self.assertIn("返済期日が権利落ち日より前", err)
        code, _, err = self.run_main(["position", "add", "X001", "--method", "nope",
                                      "--date", "2026-10-16"])
        self.assertIn("--method", err)
        code, _, err = self.run_main(["position", "add", "X001", "--method", "sbi_short",
                                      "--date", "2026-10-10"])
        self.assertIn("休場日", err)

    def test_today_uses_positions_and_budget_flag(self):
        self.run_main(["position", "add", "X001", "--method", "rakuten_short",
                       "--date", "2026-10-16", "--price", "1850"])
        code, out, _ = self.run_main(["today", "--today", "2026-10-29"])
        self.assertIn("保有中の建玉", out)
        self.assertNotIn("X003", out)  # 見送り銘柄の現渡し通知は出さない
        code, out, _ = self.run_main(["plan", "--today", "2026-10-06", "--horizon", "100", "--budget", "600000"])
        self.assertIn("資金枠（600,000円）", out)

    def test_check_and_calendar(self):
        code, out, _ = self.run_main(["check", "--today", "2026-10-01"])
        self.assertEqual(code, 0, out)
        with open("mydata/watchlist.csv", "a", encoding="utf-8") as f:
            f.write("Z001,テスト,3月9日,末,100,1000,,1000\n")
        code, out, _ = self.run_main(["check", "--today", "2026-10-01"])
        self.assertEqual(code, 1)
        self.assertIn("権利月が読めません", out)
        code, out, _ = self.run_main(["calendar", "--start", "2026-12-01", "--months", "1", "--month-end"])
        self.assertIn("2026-12-16(水)", out)  # 楽天短期初日


if __name__ == "__main__":
    unittest.main()
