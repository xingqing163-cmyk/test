import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from yutai_cross.config import Settings, settings_from_dict
from yutai_cross.jpx_calendar import MarketCalendar
from yutai_cross.planner import build_plan
from yutai_cross.prices import fetch_prices, parse_yahoo_chart
from yutai_cross.report import render_console, render_csv, render_ics, render_markdown
from yutai_cross.tax import parse_executed, tax_memo
from yutai_cross.watchlist import load_price_csv, load_watchlist, parse_watchlist

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


class WatchlistTest(unittest.TestCase):
    def test_japanese_headers(self):
        text = (
            "# コメント\n"
            "コード,銘柄名,権利月,権利日,必要株数,株価,優待価値,換金価値,1株配当,貸借,一般信用,人気\n"
            "1234,テスト,5・11,20日,200,\"1,500\",2000,1500,12.5,○,なし,高\n"
        )
        it = parse_watchlist(text)[0]
        self.assertEqual(it.record_months, [5, 11])
        self.assertEqual(it.record_day, "20日")
        self.assertEqual(it.shares, 200)
        self.assertEqual(it.price, 1500)
        self.assertEqual(it.dividend, 12.5)
        self.assertTrue(it.taishaku)
        self.assertEqual(it.ippan_brokers, [])
        self.assertEqual(it.popularity, "高")

    def test_english_headers_and_defaults(self):
        it = parse_watchlist("code,record_month,benefit_value\n130A,3,1000\n")[0]
        self.assertEqual((it.code, it.name, it.shares, it.record_day), ("130A", "130A", 100, "末"))
        self.assertIsNone(it.ippan_brokers)
        self.assertFalse(it.taishaku)

    def test_errors_have_line_numbers(self):
        with self.assertRaisesRegex(ValueError, "3行目"):
            parse_watchlist("code,record_month,benefit_value\n1,3,1\n2,13,1\n")
        with self.assertRaisesRegex(ValueError, "必須の列"):
            parse_watchlist("code,benefit_value\n1,1\n")

    def test_cp932_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "w.csv"
            p.write_bytes("コード,権利月,優待価値\n1234,3,1000\n".encode("cp932"))
            self.assertEqual(load_watchlist(p)[0].code, "1234")
            p2 = Path(d) / "p.csv"
            p2.write_text("コード,株価\n1234,\"2,000\"\n", encoding="utf-8")
            self.assertEqual(load_price_csv(p2), {"1234": 2000.0})

    def test_sample_watchlist_loads(self):
        items = load_watchlist(EXAMPLES / "watchlist_sample.csv")
        self.assertEqual(len(items), 8)


class SettingsTest(unittest.TestCase):
    def test_example_settings_match_defaults(self):
        with open(EXAMPLES / "settings.example.json", encoding="utf-8") as f:
            s = settings_from_dict(json.load(f))
        d = Settings()
        self.assertEqual([m.id for m in s.methods], [m.id for m in d.methods])
        self.assertEqual(s.min_profit_yen, d.min_profit_yen)
        self.assertEqual(s.max_lookback_business_days, d.max_lookback_business_days)
        self.assertEqual(s.popularity_premium, d.popularity_premium)

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            settings_from_dict({"min_profit": 1})
        with self.assertRaises(ValueError):
            settings_from_dict({"valuation": "x"})


class PricesTest(unittest.TestCase):
    def test_parse(self):
        payload = json.dumps({"chart": {"result": [{"meta": {"regularMarketPrice": 2345.0}}]}})
        self.assertEqual(parse_yahoo_chart(payload.encode()), 2345.0)
        payload = json.dumps({"chart": {"result": [
            {"meta": {}, "indicators": {"quote": [{"close": [1.0, None, 3.0, None]}]}}]}})
        self.assertEqual(parse_yahoo_chart(payload.encode()), 3.0)
        self.assertIsNone(parse_yahoo_chart(b'{"chart": {"result": null}}'))

    def test_fetch_with_errors(self):
        def fake(url):
            if "9999" in url:
                raise OSError("blocked")
            return json.dumps({"chart": {"result": [{"meta": {"regularMarketPrice": 100}}]}}).encode()

        prices, errors = fetch_prices(["1234", "9999", "1234"], fetch=fake, wait_seconds=0)
        self.assertEqual(prices, {"1234": 100.0})
        self.assertEqual(len(errors), 1)


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = Settings(horizon_days=100)
        cls.cal = MarketCalendar()
        items = load_watchlist(EXAMPLES / "watchlist_sample.csv")
        cls.result = build_plan(items, date(2026, 10, 6), cls.settings, cal=cls.cal)

    def test_console(self):
        text = render_console(self.result, self.settings, self.cal)
        self.assertIn("おすすめ候補", text)
        self.assertIn("短期解禁", text)
        self.assertIn("見送り", text)

    def test_markdown(self):
        md = render_markdown(self.result, self.settings, self.cal)
        self.assertIn("現渡し", md)
        self.assertIn("| 方法 |", md)

    def test_csv(self):
        lines = render_csv(self.result, self.settings).splitlines()
        self.assertTrue(lines[0].startswith("判定,状態,コード"))
        self.assertEqual(len(lines), 1 + len(self.result.plans))

    def test_ics(self):
        ics = render_ics(self.result, self.settings, now=datetime(2026, 10, 6, tzinfo=timezone.utc))
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("DTSTART;VALUE=DATE:20261028", ics)
        for line in ics.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)


class TaxTest(unittest.TestCase):
    def test_memo(self):
        text = (EXAMPLES / "executed_sample.csv").read_text(encoding="utf-8")
        memo = tax_memo(parse_executed(text), 2026)
        self.assertIn("雑所得（その他）の収入金額: 7,500円", memo)
        self.assertIn("合計: 599円", memo)
        self.assertIn("売却額合計: 2,400円", memo)

    def test_income_falls_back_to_sold(self):
        recs = parse_executed("受取日,コード,評価額,売却額\n2026/03/01,1,,800\n")
        self.assertEqual(recs[0].income, 800)


class CliTest(unittest.TestCase):
    def test_plan_and_init(self):
        from yutai_cross.cli import main

        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            cwd = os.getcwd()
            os.chdir(d)
            try:
                self.assertEqual(main(["init"]), 0)
                self.assertTrue(Path("mydata/watchlist.csv").exists())
                self.assertEqual(main(["plan", "--today", "2026-10-06", "-o", "out"]), 0)
                self.assertTrue(Path("out/plan_20261006.md").exists())
                self.assertTrue(Path("out/yutai_cross.ics").exists())
                self.assertEqual(main(["today", "--today", "2026-10-06"]), 0)
                self.assertEqual(main(["dates", "2026-12-31"]), 0)
                self.assertEqual(main(["tax", "--year", "2026"]), 0)
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
