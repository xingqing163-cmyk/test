"""専門家レビューで見つかった問題の回帰テスト。"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from yutai_cross.cli import main
from yutai_cross.config import Settings, load_settings, settings_from_dict
from yutai_cross.costs import quote
from yutai_cross.jpx_calendar import MarketCalendar
from yutai_cross.planner import (
    actions_on,
    build_plan,
    short_due_date,
    short_window_start,
)
from yutai_cross.report import render_ics
from yutai_cross.tax import parse_executed, tax_memo
from yutai_cross.watchlist import WatchItem, parse_watchlist

ROOT = Path(__file__).resolve().parent.parent
HEADER = "コード,銘柄名,権利月,権利日,株価,優待価値,一般信用,人気\n"


def methods():
    return {m.id: m for m in Settings().methods}


class ShortTermWindowTest(unittest.TestCase):
    """楽天「短期(14日)」は暦日、SBI「短期(15営業日)」は営業日で数える。"""

    def setUp(self):
        self.cal = MarketCalendar()

    def start(self, record, method_id):
        rd = self.cal.rights_dates(record)
        return short_window_start(self.cal, rd.ex_date, methods()[method_id])

    def test_rakuten_calendar_days(self):
        # 楽天の告知「31日が月末最終売買日の場合、約定日は17日から」（2026年3月末）
        self.assertEqual(self.start(date(2026, 3, 31), "rakuten_short"), date(2026, 3, 17))
        self.assertEqual(self.start(date(2027, 7, 31), "rakuten_short"), date(2027, 7, 16))
        self.assertEqual(self.start(date(2026, 12, 31), "rakuten_short"), date(2026, 12, 16))
        due = short_due_date(self.cal, date(2026, 3, 17), methods()["rakuten_short"])
        self.assertEqual(due, date(2026, 3, 30))  # 権利落ち日と同じ日が返済期日

    def test_rakuten_start_moves_forward_when_holiday(self):
        # 2026-10-31 → 落ち日 10/29、13日前は 10/16(金)。2026-11-30 → 落ち日 11/27、13日前は 11/14(土)→11/16(月)
        self.assertEqual(self.start(date(2026, 10, 31), "rakuten_short"), date(2026, 10, 16))
        self.assertEqual(self.start(date(2026, 11, 30), "rakuten_short"), date(2026, 11, 16))

    def test_sbi_business_days(self):
        self.assertEqual(self.start(date(2026, 12, 31), "sbi_short"), date(2026, 12, 8))
        due = short_due_date(self.cal, date(2026, 12, 8), methods()["sbi_short"])
        self.assertEqual(due, date(2026, 12, 29))

    def test_legacy_setting_name(self):
        s = settings_from_dict({"methods": [{"id": "x", "broker": "X", "label": "x",
                                             "max_hold_business_days": 15}]})
        self.assertEqual((s.methods[0].max_hold_days, s.methods[0].hold_basis), (15, "business"))


class WatchlistInputTest(unittest.TestCase):
    def one(self, row, header=HEADER):
        return parse_watchlist(header + row + "\n")[0]

    def test_fullwidth_and_currency(self):
        it = self.one("１２３４,テスト,３,末,\"￥1,850\",\"1,000円\",,")
        self.assertEqual(it.code, "1234")
        self.assertEqual(it.record_months, [3])
        self.assertEqual(it.price, 1850.0)
        self.assertEqual(it.benefit_value, 1000)

    def test_header_with_unit_suffix(self):
        header = "コード,権利月,優待価値（円）,1株配当(円)\n"
        it = self.one("1234,3,1000,-", header=header)
        self.assertEqual((it.benefit_value, it.dividend), (1000, 0.0))

    def test_record_day_variants(self):
        self.assertEqual(self.one("1,2,3,月末,100,1000,,").record_day, "末")
        self.assertEqual(self.one("1,2,3,20日,100,1000,,").record_day, "20")
        with self.assertRaisesRegex(ValueError, "権利日"):
            self.one("1,2,3,中旬,100,1000,,")

    def test_broker_column_variants(self):
        self.assertIsNone(self.one("1,2,3,末,100,1000,○,").ippan_brokers)
        self.assertEqual(self.one("1,2,3,末,100,1000,SBI証券(一般・短期),").ippan_brokers, ["SBI短期"])
        self.assertEqual(self.one("1,2,3,末,100,1000,ＳＢＩ 短期;楽天無期限,").ippan_brokers,
                         ["SBI短期", "楽天無期限"])

    def test_unknown_broker_warns(self):
        it = self.one("1,2,10,末,1000,3000,マネックス,")
        p = build_plan([it], date(2026, 10, 1), Settings()).plans[0]
        self.assertTrue(any("マネックス を読めません" in w for w in p.warnings))

    def test_popularity_normalized(self):
        self.assertEqual(self.one("1,2,3,末,100,1000,,大").popularity, "高")
        it = self.one("1,2,3,末,100,1000,,すごく")
        self.assertEqual(it.popularity, "")
        self.assertTrue(it.notes)

    def test_excel_line_numbers_and_cr_newlines(self):
        text = "# コメント\n# コメント\n" + HEADER + "1,2,3,末,100,1000,,\n1,2,13,末,100,1000,,\n"
        with self.assertRaisesRegex(ValueError, "5行目"):
            parse_watchlist(text)
        cr = (HEADER + "1,2,3,末,100,1000,,\n").replace("\n", "\r")
        self.assertEqual(len(parse_watchlist(cr)), 1)

    def test_month_turned_into_date_by_excel(self):
        with self.assertRaisesRegex(ValueError, "3・9"):
            self.one("1,2,3月9日,末,100,1000,,")

    def test_invalid_shares(self):
        with self.assertRaisesRegex(ValueError, "必要株数"):
            parse_watchlist("コード,権利月,優待価値,必要株数\n1,3,1000,-100\n")


class CrossedAndOrderTest(unittest.TestCase):
    def item(self, **kw):
        base = dict(code="A", name="a", record_months=[10], price=1850, benefit_value=3000,
                    ippan_brokers=["SBI短期"], popularity="低")
        base.update(kw)
        return WatchItem(**base)

    def test_order_is_short_first(self):
        r = build_plan([self.item()], date(2026, 10, 28), Settings())
        text = actions_on(date(2026, 10, 28), r)[0].text
        self.assertLess(text.index("①"), text.index("②現物買い"))
        self.assertIn("特定", text)
        self.assertIn("両方とも引成", text)

    def test_crossed_suppresses_entry_but_keeps_genwatashi(self):
        r = build_plan([self.item(crossed=True)], date(2026, 10, 28), Settings())
        self.assertEqual(actions_on(date(2026, 10, 28), r), [])
        self.assertTrue(r.plans[0].phase.startswith("クロス済み"))
        r = build_plan([self.item(crossed=True)], date(2026, 10, 29), Settings())
        self.assertEqual([a.kind for a in actions_on(date(2026, 10, 29), r)], ["現渡し"])


class SeidoEstimateTest(unittest.TestCase):
    def test_missing_estimate_is_not_zero(self):
        cal = MarketCalendar()
        rd = cal.rights_dates(date(2026, 10, 31))
        it = WatchItem(code="A", name="a", record_months=[10], price=2000, benefit_value=1000,
                       taishaku=True)
        s = Settings(seido_risk_basis="expected")
        q = quote(it, rd, methods()["seido"], rd.last_cum_date, cal, s)
        self.assertEqual(q.gyakuhibu_expected, q.gyakuhibu_worst)
        self.assertGreater(q.gyakuhibu_expected, 0)


class TaxInputTest(unittest.TestCase):
    def test_excel_dates(self):
        recs = parse_executed("受取日,コード,評価額\n2026/6/10,1,100\n2026年7月1日,2,100\n")
        self.assertEqual([r.received for r in recs], [date(2026, 6, 10), date(2026, 7, 1)])

    def test_cautions(self):
        memo = tax_memo(parse_executed("受取日,コード,評価額,売却額\n2026-06-10,1,1500,2400\n"
                                       "2026-06-11,2,,\n"), 2026)
        self.assertIn("900円の差", memo)
        self.assertIn("0円です", memo)


class SettingsFileTest(unittest.TestCase):
    def write(self, content, bom=False):
        d = tempfile.mkdtemp()
        p = Path(d) / "s.json"
        p.write_text(content, encoding="utf-8-sig" if bom else "utf-8")
        return p

    def test_bom_is_ok(self):
        self.assertEqual(load_settings(self.write('{"min_profit_yen": 500}', bom=True)).min_profit_yen, 500)

    def test_syntax_error_names_file_and_line(self):
        with self.assertRaisesRegex(ValueError, "s.json の 1行目"):
            load_settings(self.write('{"min_profit_yen": 500,}'))

    def test_type_error_is_friendly(self):
        with self.assertRaisesRegex(ValueError, "horizon_days の値の型"):
            load_settings(self.write('{"horizon_days": "60"}'))

    def test_example_settings_use_calendar_basis_for_rakuten(self):
        with open(ROOT / "examples" / "settings.example.json", encoding="utf-8") as f:
            s = settings_from_dict(json.load(f))
        rakuten = next(m for m in s.methods if m.id == "rakuten_short")
        self.assertEqual((rakuten.max_hold_days, rakuten.hold_basis), (14, "calendar"))


class IcsTest(unittest.TestCase):
    def test_uid_unique_and_no_duplicate_open_event(self):
        items = [WatchItem(code="A", name="a", record_months=[10], price=1850, benefit_value=3000,
                           ippan_brokers=["SBI短期"], popularity="高", shares=n) for n in (100, 500)]
        r = build_plan(items, date(2026, 10, 1), Settings())
        ics = render_ics(r, Settings(), now=datetime(2026, 10, 1, tzinfo=timezone.utc))
        uids = [l for l in ics.split("\r\n") if l.startswith("UID:")]
        self.assertEqual(len(uids), len(set(uids)))
        # 100株は短期初日＝エントリー目安日なので「短期初日」の予定を重複させない
        self.assertNotIn("UID:A-100-2026-10-31-open", ics)
        # 500株は利益が薄く目安日が後ろにずれるので、短期初日の予定も入る
        self.assertIn("UID:A-500-2026-10-31-open", ics)


class CliRobustnessTest(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        self.wl = str(ROOT / "examples" / "watchlist_sample.csv")

    def tearDown(self):
        os.chdir(self.cwd)

    def test_missing_prices_file(self):
        code, _, err = self.run_main(["plan", "-w", self.wl, "--prices", "nope.csv"])
        self.assertEqual(code, 2)
        self.assertIn("ファイルが見つかりません", err)

    def test_broken_config(self):
        Path("bad.json").write_text("{", encoding="utf-8")
        code, _, err = self.run_main(["plan", "-w", self.wl, "-c", "bad.json"])
        self.assertEqual(code, 2)
        self.assertIn("bad.json", err)

    def test_init_copies_no_fake_records(self):
        with contextlib.redirect_stdout(io.StringIO()):
            os.chdir(self.cwd)  # examples を見つけられる場所から
            os.chdir(self.tmp)
        code, _, _ = self.run_main(["init"])
        self.assertEqual(code, 0)
        executed = Path("mydata/executed.csv").read_text(encoding="utf-8-sig").strip().splitlines()
        self.assertEqual(len(executed), 1)  # 見出しだけ
        self.assertTrue(Path("mydata/watchlist.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_dates_shows_rakuten_calendar_day(self):
        code, out, _ = self.run_main(["dates", "2026-12-31"])
        self.assertEqual(code, 0)
        self.assertIn("楽天証券 一般信用(短期)（短期(14日)）の建て可能開始日（短期初日）: 2026-12-16(水)", out)

    def test_cost_rejects_entry_before_window(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                main(["cost", "--record", "2026-12-31", "--price", "2400", "--benefit", "1000",
                      "--entry", "2026-12-01"])

    def test_today_shows_next_schedule_on_quiet_day(self):
        code, out, _ = self.run_main(["today", "-w", self.wl, "--today", "2026-10-01"])
        self.assertEqual(code, 0)
        self.assertIn("■ 次の予定:", out)

    def test_ics_written_with_crlf_only(self):
        code, _, _ = self.run_main(["plan", "-w", self.wl, "--today", "2026-10-06", "-o", "out"])
        self.assertEqual(code, 0)
        raw = Path("out/yutai_cross.ics").read_bytes()
        self.assertNotIn(b"\r\r\n", raw)
        self.assertIn(b"\r\n", raw)


if __name__ == "__main__":
    unittest.main()
