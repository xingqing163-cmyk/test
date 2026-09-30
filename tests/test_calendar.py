import unittest
from datetime import date

from yutai_cross.jpx_calendar import (
    MarketCalendar,
    fmt_date,
    japanese_holidays,
    record_date_for,
)


class HolidayTest(unittest.TestCase):
    def test_2026_holidays(self):
        expected = {
            date(2026, 1, 1), date(2026, 1, 12), date(2026, 2, 11), date(2026, 2, 23),
            date(2026, 3, 20), date(2026, 4, 29), date(2026, 5, 3), date(2026, 5, 4),
            date(2026, 5, 5), date(2026, 5, 6), date(2026, 7, 20), date(2026, 8, 11),
            date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 10, 12),
            date(2026, 11, 3), date(2026, 11, 23),
        }
        self.assertEqual(set(japanese_holidays(2026)), expected)
        self.assertEqual(japanese_holidays(2026)[date(2026, 5, 6)], "振替休日")
        self.assertEqual(japanese_holidays(2026)[date(2026, 9, 22)], "国民の休日")

    def test_2027_substitute_for_vernal_equinox_on_sunday(self):
        h = japanese_holidays(2027)
        self.assertEqual(h[date(2027, 3, 21)], "春分の日")
        self.assertEqual(h[date(2027, 3, 22)], "振替休日")

    def test_out_of_range(self):
        with self.assertRaises(ValueError):
            japanese_holidays(2019)


class MarketCalendarTest(unittest.TestCase):
    def setUp(self):
        self.cal = MarketCalendar()

    def test_business_days(self):
        self.assertFalse(self.cal.is_business_day(date(2026, 12, 31)))  # 大晦日は休場
        self.assertFalse(self.cal.is_business_day(date(2027, 1, 2)))
        self.assertFalse(self.cal.is_business_day(date(2026, 10, 12)))  # スポーツの日
        self.assertTrue(self.cal.is_business_day(date(2026, 12, 30)))   # 大納会
        self.assertEqual(self.cal.add_business_days(date(2026, 12, 30), 1), date(2027, 1, 4))
        self.assertEqual(self.cal.add_business_days(date(2026, 10, 13), -1), date(2026, 10, 9))
        self.assertEqual(self.cal.business_days_between(date(2026, 10, 9), date(2026, 10, 14)), 2)

    def test_extra_holiday(self):
        cal = MarketCalendar(extra_holidays=[date(2026, 10, 13)])
        self.assertEqual(cal.add_business_days(date(2026, 10, 9), 1), date(2026, 10, 14))
        self.assertEqual(cal.holiday_name(date(2026, 10, 13)), "臨時休場")

    def check(self, record, last_cum, ex):
        rd = self.cal.rights_dates(record)
        self.assertEqual(rd.last_cum_date, last_cum, fmt_date(record))
        self.assertEqual(rd.ex_date, ex, fmt_date(record))

    def test_rights_dates(self):
        # 平日の月末
        self.check(date(2026, 9, 30), date(2026, 9, 28), date(2026, 9, 29))
        self.check(date(2026, 3, 31), date(2026, 3, 27), date(2026, 3, 30))
        self.check(date(2027, 3, 31), date(2027, 3, 29), date(2027, 3, 30))
        # 月末が土曜 → 実質の基準日は金曜
        self.check(date(2026, 10, 31), date(2026, 10, 28), date(2026, 10, 29))
        # 月末が日曜
        self.check(date(2026, 5, 31), date(2026, 5, 27), date(2026, 5, 28))
        # 大晦日は休場なので 12/30 が実質の基準日
        self.check(date(2026, 12, 31), date(2026, 12, 28), date(2026, 12, 29))
        # 間に祝日（昭和の日）をはさむ
        self.check(date(2026, 4, 30), date(2026, 4, 27), date(2026, 4, 28))
        # 20日基準、落ち日の後に連休
        self.check(date(2026, 11, 20), date(2026, 11, 18), date(2026, 11, 19))

    def test_settlement_days_configurable(self):
        cal = MarketCalendar(settlement_days=1)  # 将来 T+1 になった場合
        rd = cal.rights_dates(date(2026, 9, 30))
        self.assertEqual(rd.last_cum_date, date(2026, 9, 29))
        self.assertEqual(rd.ex_date, date(2026, 9, 30))


class RecordDateTest(unittest.TestCase):
    def test_specs(self):
        self.assertEqual(record_date_for(2026, 2, "末"), date(2026, 2, 28))
        self.assertEqual(record_date_for(2028, 2, "末"), date(2028, 2, 29))
        self.assertEqual(record_date_for(2026, 8, "20"), date(2026, 8, 20))
        self.assertEqual(record_date_for(2026, 8, "20日"), date(2026, 8, 20))
        self.assertEqual(record_date_for(2026, 4, 31), date(2026, 4, 30))
        with self.assertRaises(ValueError):
            record_date_for(2026, 4, "0")


if __name__ == "__main__":
    unittest.main()
