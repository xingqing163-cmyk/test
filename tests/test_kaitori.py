import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest import mock

from kaitori_check import cli
from kaitori_check.buyback import (
    BuybackItem,
    fetch_site,
    items_to_csv,
    page_links,
    parse_cards,
    parse_embedded,
    parse_items_csv,
    parse_jsonld,
    parse_listing,
    pick_price,
    price_candidates,
    robots_allows,
    with_page,
)
from kaitori_check.config import Settings, load_settings, settings_from_dict
from kaitori_check.matching import (
    clean_query,
    color_caution,
    color_conflict,
    has_model_number,
    kit_mismatch,
    name_matches,
)
from kaitori_check.profit import (
    JUDGE_GOOD,
    JUDGE_LOSS,
    JUDGE_NONE,
    JUDGE_THIN,
    MATCH_JAN,
    MATCH_NAME,
    calc,
    check_item,
    evaluate,
    judge,
)
from kaitori_check.report import render_console, render_csv, render_html
from kaitori_check.yahoo import Offer, YahooError, build_url, parse_response, search

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://kaitori.example.com/products?category=abc"


class SettingsTest(unittest.TestCase):
    def test_example_loads(self):
        s = load_settings(ROOT / "examples" / "kaitori_settings.example.json")
        self.assertEqual(s.tax_mode, "exempt")
        self.assertEqual(len(s.enabled_sites()), 1)

    def test_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            settings_from_dict({"tax_mode": "foo"})
        with self.assertRaises(ValueError):
            settings_from_dict({"extra_point_rate": 5})  # 5% は 0.05 と書く
        with self.assertRaises(ValueError):
            settings_from_dict({"yahoo_interval_sec": 0})
        with self.assertRaises(ValueError):
            settings_from_dict({"sites": [{"name": "x", "url": "kaitori.example.com"}]})
        with self.assertRaises(ValueError):
            settings_from_dict({"unknown": 1})

    def test_appid_from_env(self):
        with mock.patch.dict(os.environ, {"YAHOO_APPID": "env-id"}):
            self.assertEqual(Settings().appid(), "env-id")
            self.assertEqual(Settings(yahoo_appid="file-id").appid(), "file-id")


class MatchingTest(unittest.TestCase):
    def test_same_model_with_spacing_and_mark_variants(self):
        self.assertTrue(name_matches("Canon EOS R6 Mark II ボディ", "キヤノン EOS R6 MarkII ボディ 新品"))
        self.assertTrue(name_matches("Nikon Z6III", "ニコン Z 6III ボディ"))
        self.assertTrue(name_matches("SONY α7 IV ILCE-7M4 ボディ", "ソニー α7IV ILCE7M4 ミラーレス"))
        self.assertTrue(name_matches("RF24-105mm F4 L IS USM", "キヤノン RF24-105mm F4L IS USM 交換レンズ"))

    def test_different_generation_or_model(self):
        self.assertFalse(name_matches("Canon EOS R6 Mark II", "キヤノン EOS R6 ボディ"))
        self.assertFalse(name_matches("SONY α7", "ソニー α7 IV ボディ"))
        self.assertFalse(name_matches("FE 24-70mm F2.8 GM", "ソニー FE 24-70mm F2.8 GM II SEL2470GM2"))
        self.assertFalse(name_matches("Nikon Z6II", "ニコン Z6III"))
        self.assertFalse(name_matches("RF24-105mm F4 L IS USM", "キヤノン RF24-105mm F4-7.1 IS STM"))

    def test_needs_model_number(self):
        self.assertFalse(has_model_number("キヤノン ミラーレス一眼 ボディ"))
        self.assertFalse(name_matches("キヤノン ミラーレス一眼", "キヤノン ミラーレス一眼"))

    def test_kit_and_color(self):
        self.assertIsNotNone(kit_mismatch("EOS R8 ボディ", "EOS R8 RF24-50 レンズキット"))
        self.assertIsNotNone(kit_mismatch("EOS R8 RF24-50 レンズキット", "EOS R8 ボディ"))
        self.assertIsNone(kit_mismatch("EOS R8 ボディ", "EOS R8 ボディ"))
        self.assertIsNotNone(color_caution("X100VI シルバー", "富士フイルム X100VI ブラック"))
        self.assertIsNone(color_caution("X100VI シルバー", "FUJIFILM X100VI SILVER"))
        self.assertIsNone(color_caution("X100VI", "X100VI ブラック"))
        self.assertTrue(color_conflict("X100VI シルバー", "X100VI ブラック"))
        self.assertFalse(color_conflict("X100VI シルバー", "X100VI"))
        self.assertFalse(color_conflict("X100VI シルバー", "X100VI シルバー/ブラック 2色"))

    def test_clean_query(self):
        self.assertEqual(clean_query("【新品未開封】Canon EOS R6 Mark II（ボディ）"), "Canon EOS R6 Mark II ボディ")


class PriceTextTest(unittest.TestCase):
    def test_prefers_new_price(self):
        c = price_candidates("新品未開封 ¥250,000 開封済み ¥200,000")
        self.assertEqual(pick_price(c), (250000, "新品未開封"))
        c = price_candidates("中古 210,000円 新品 230,000円")
        self.assertEqual(pick_price(c)[0], 230000)

    def test_ignores_non_buyback_amounts(self):
        c = price_candidates("買取価格 ¥98,000 （送料無料 ¥5,000以上） 定価 ¥120,000")
        self.assertEqual([p for _, p in c], [98000])


NEXT_DATA_HTML = """<html><body><div id="__next"></div>
<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{
 "category":{"name":"カメラ","id":"abc"},
 "products":[
  {"id":"p1","name":"Canon EOS R6 Mark II ボディ","janCode":"4549292204605","prices":[
     {"condition":"新品未開封","price":285000},{"condition":"開封済み","price":250000}]},
  {"id":"p2","name":"SONY FE 24-70mm F2.8 GM II","jan":"4548736143241","newPrice":"248,000","usedPrice":200000},
  {"id":"p3","name":"送料","price":0}
 ]}}}</script></body></html>"""


def flight_html(rows):
    pushes = "".join(
        f"<script>self.__next_f.push([1,{json.dumps(r)}])</script>" for r in rows
    )
    return f"<html><body>{pushes}</body></html>"


CARDS_HTML = """<html><body><header><a href="/">トップ</a> 送料無料キャンペーン ¥5,000以上</header>
<ul class="list">
 <li class="card"><a href="/products/1"><h3 class="name">Nikon Z6III ボディ</h3></a>
   <p>JAN: 4960759916406</p><div class="price"><span>新品</span> ¥268,000</div><div class="price"><span>開封</span> ¥240,000</div></li>
 <li class="card"><a href="/products/2"><h3 class="name">FUJIFILM X100VI シルバー</h3></a>
   <p>JAN: 4547410520028</p><div class="price"><span>新品</span> ¥231,000</div></li>
 <li class="card"><a href="/products/3"><h3 class="name">SIGMA 24-70mm F2.8 DG DN II | Art ソニーE</h3></a>
   <div class="price"><span>新品</span> 135,000円</div></li>
</ul>
<footer>振込手数料 ¥0</footer></body></html>"""

TABLE_HTML = """<table><tr><th>商品名</th><th>JAN</th><th>新品買取</th></tr>
<tr><td>Canon RF24-105mm F4 L IS USM</td><td>4549292157321</td><td>¥118,000</td></tr>
<tr><td>Canon RF70-200mm F2.8 L IS USM Z</td><td>4549292211580</td><td>¥335,000</td></tr>
</table>"""


class ParseTest(unittest.TestCase):
    def test_next_data(self):
        items = parse_embedded(NEXT_DATA_HTML, "SY", BASE)
        self.assertEqual([(i.name, i.price, i.jan) for i in items], [
            ("Canon EOS R6 Mark II ボディ", 285000, "4549292204605"),
            ("SONY FE 24-70mm F2.8 GM II", 248000, "4548736143241"),
        ])

    def test_category_with_price_is_not_taken_as_product(self):
        data = {"category": {"name": "カメラ本体", "minPrice": 50000, "items": [
            {"name": "Canon EOS R8 ボディ", "price": 150000,
             "variants": [{"name": "新品未開封", "price": 150000}, {"name": "開封済み", "price": 130000}]},
            {"name": "Nikon Z5II ボディ", "price": 170000},
        ]}}
        html = f'<script type="application/json">{json.dumps(data)}</script>'
        items = parse_embedded(html, "SY", BASE)
        self.assertEqual([(i.name, i.price) for i in items], [("Canon EOS R8 ボディ", 150000),
                                                              ("Nikon Z5II ボディ", 170000)])

    def test_next_flight_data(self):
        payload = {"products": [{"title": "Nikon Z8", "jan_code": "4960759910381",
                                 "buybackPrice": 480000, "listPrice": 600000}]}
        html = flight_html(['0:["$","div",null,{}]\n', "5:" + json.dumps(payload)[:30],
                            json.dumps(payload)[30:] + "\n"])
        items = parse_embedded(html, "SY", BASE)
        self.assertEqual([(i.name, i.price, i.jan) for i in items], [("Nikon Z8", 480000, "4960759910381")])

    def test_jsonld(self):
        data = {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "item": {"@type": "Product", "name": "OM SYSTEM OM-1 Mark II",
                                           "gtin13": "4545350056064", "url": "/p/om1",
                                           "offers": {"@type": "Offer", "price": "238000"}}}]}
        html = f'<script type="application/ld+json">{json.dumps(data)}</script>'
        items = parse_jsonld(html, "SY", BASE)
        self.assertEqual(items[0].price, 238000)
        self.assertEqual(items[0].url, "https://kaitori.example.com/p/om1")

    def test_cards(self):
        items = parse_cards(CARDS_HTML, "SY", BASE)
        self.assertEqual([(i.name, i.price, i.jan) for i in items], [
            ("Nikon Z6III ボディ", 268000, "4960759916406"),
            ("FUJIFILM X100VI シルバー", 231000, "4547410520028"),
            ("SIGMA 24-70mm F2.8 DG DN II | Art ソニーE", 135000, ""),
        ])
        self.assertEqual(items[0].url, "https://kaitori.example.com/products/1")

    def test_cards_with_price_rows_inside(self):
        # 価格の行（新品・未開封 / 開封・未使用）がカードより多く並んでいても、カード単位で読む
        card = ('<div class="group rounded-lg border p-3"><a class="block" href="/products/{i}">'
                '<p class="text-xs">JAN {jan}</p><p class="font-bold">{name}</p></a>'
                '<div class="space-y-1"><div class="flex justify-between"><span>新品・未開封</span><span>¥{new:,}</span></div>'
                '<div class="flex justify-between"><span>開封・未使用</span><span>¥{used:,}</span></div></div>'
                '<button>カートに入れる</button></div>')
        html = ('<nav><a href="/products?category=c1">レンズ (120)</a></nav><p>送料無料（¥10,000以上）</p><div class="grid">'
                + card.format(i=1, jan="4549292204605", name="Canon EOS R6 Mark II ボディ", new=300000, used=280000)
                + card.format(i=2, jan="4548736133730", name="SONY α7 IV ILCE-7M4 ボディ", new=260000, used=240000)
                + "</div>")
        items, _ = parse_listing(html, "SY", BASE)
        self.assertEqual([(i.name, i.price, i.condition, i.jan) for i in items], [
            ("Canon EOS R6 Mark II ボディ", 300000, "新品・未開封", "4549292204605"),
            ("SONY α7 IV ILCE-7M4 ボディ", 260000, "新品・未開封", "4548736133730"),
        ])

    def test_table(self):
        items, parser = parse_listing(TABLE_HTML, "SY", BASE)
        self.assertEqual(parser, "HTMLの商品カード")
        self.assertEqual([(i.price, i.jan) for i in items], [(118000, "4549292157321"), (335000, "4549292211580")])

    def test_listing_picks_best_parser(self):
        items, parser = parse_listing(NEXT_DATA_HTML, "SY", BASE)
        self.assertEqual(parser, "埋め込みJSON")
        self.assertEqual(len(items), 2)
        self.assertEqual(parse_listing("<html><body>読み込み中…</body></html>", "SY", BASE), ([], ""))


def card_page(n, names, links=""):
    cards = "".join(f'<div class="c"><h3>{name}</h3><span>新品 ¥{100000 + i:,}</span></div>'
                    for i, name in enumerate(names))
    return f"<html><body>{cards}{links}</body></html>".encode()


class FetchTest(unittest.TestCase):
    def test_pagination_stops_when_nothing_new(self):
        links = '<a href="?category=abc&page=2">2</a><a href="?category=abc&page=3">3</a>'
        pages = {
            BASE: card_page(1, ["A1 Z5", "A2 Z6"], links),
            with_page(BASE, 2): card_page(2, ["B1 Z7", "B2 Z8"], links),
            with_page(BASE, 3): card_page(3, ["A1 Z5", "A2 Z6"], links),  # 範囲外は1ページ目と同じ
        }
        calls = []

        def fetch(url):
            calls.append(url)
            return pages[url]

        items = fetch_site("SY", BASE, "カメラ", fetch=fetch, interval=0, max_pages=10)
        self.assertEqual([i.name for i in items], ["A1 Z5", "A2 Z6", "B1 Z7", "B2 Z8"])
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(i.category == "カメラ" and i.fetched_at for i in items))

    def test_later_page_error_keeps_earlier_items(self):
        def fetch(url):
            if url == BASE:
                return card_page(1, ["A1 Z5", "A2 Z6"])
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        logs = []
        items = fetch_site("SY", BASE, fetch=fetch, interval=0, on_page=logs.append)
        self.assertEqual(len(items), 2)
        self.assertIn("取得できず", logs[-1].parser)

    def test_page_links(self):
        html = '<a href="/products?category=abc&amp;page=4">4</a><a href="/other?page=9">x</a>'
        self.assertEqual(page_links(html, BASE), {4: "https://kaitori.example.com/products?category=abc&page=4"})

    def test_robots(self):
        def fetch(url):
            self.assertTrue(url.endswith("/robots.txt"))
            return b"User-agent: *\nDisallow: /products\n"
        self.assertFalse(robots_allows(BASE, fetch))

        def missing(url):
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        self.assertTrue(robots_allows(BASE, missing))


class CsvTest(unittest.TestCase):
    def test_roundtrip_and_excel_safety(self):
        items = [BuybackItem(site="SY", name="=HYPERLINK(\"x\")", price=12345, jan="4549292204605"),
                 BuybackItem(site="SY", name="EOS R8", price=150000)]
        text = items_to_csv(items)
        self.assertIn("'=HYPERLINK", text)
        back = parse_items_csv(text)
        self.assertEqual([(i.name, i.price, i.jan) for i in back],
                         [(i.name, i.price, i.jan) for i in items])

    def test_handmade_csv(self):
        items = parse_items_csv("# 手入力\n商品名,買取価格,JAN\nEOS R8,\"¥150,000\",４５４９２９２２０４６０５\n")
        self.assertEqual((items[0].price, items[0].jan), (150000, "4549292204605"))
        with self.assertRaises(ValueError):
            parse_items_csv("商品名,価格\nEOS R8,未定\n")


def hit(name, price, *, amount=0, bonus=0, ship="送料無料", rate=4.6, count=1200, cond="new", **kw):
    h = {"name": name, "price": price, "url": "https://store.shopping.yahoo.co.jp/x/y.html",
         "condition": cond, "inStock": True, "janCode": "4549292204605",
         "point": {"amount": amount, "bonusAmount": bonus},
         "shipping": {"code": 2, "name": ship},
         "seller": {"name": "テストストア", "review": {"rate": rate, "count": count}}}
    h.update(kw)
    return h


class YahooTest(unittest.TestCase):
    def test_build_url(self):
        url = build_url("ID", jan="4549292204605", results=30)
        self.assertIn("jan_code=4549292204605", url)
        self.assertIn("condition=new", url)
        self.assertIn("sort=%2Bprice", url)
        self.assertNotIn("query=", url)
        self.assertIn("price_from=1000", build_url("ID", query="EOS R8", price_from=1000))

    def test_parse(self):
        payload = json.dumps({"hits": [
            hit("A", 100000, amount=909, bonus=4545),
            hit("B", 99000, ship="送料別", premiumPrice=95000, premiumPriceStatus=True),
            hit("C", 50000, cond="used"),
        ]}).encode()
        offers = parse_response(payload)
        self.assertEqual([(o.name, o.price, o.points, o.shipping_free) for o in offers],
                         [("A", 100000, 4545, True), ("B", 99000, 0, False)])
        self.assertEqual(parse_response(payload, premium=True)[1].price, 95000)
        with self.assertRaises(YahooError):
            parse_response(b'{"Error":{"Message":"invalid appid"}}')

    def test_http_errors(self):
        def forbidden(url):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b'{"Error":{"Message":"bad"}}'))
        with self.assertRaisesRegex(YahooError, "アプリケーションID"):
            search("ID", jan="1", fetch=forbidden)


class ProfitTest(unittest.TestCase):
    def test_exempt(self):
        b = calc(105000, 98000, Settings(), api_points=890)
        self.assertEqual((b.cash_profit, b.profit, b.consumption_tax), (7000, 7890, 0))
        self.assertEqual(judge(b, Settings()), JUDGE_GOOD)

    def test_tax_modes(self):
        s = Settings(tax_mode="general", send_shipping_yen=1100)
        b = calc(110000, 99000, s)
        # (110000 - 99000 - 1100) × 10/110 = 900
        self.assertEqual(b.consumption_tax, 900)
        self.assertEqual(calc(110000, 99000, Settings(tax_mode="simplified")).consumption_tax, 1000)
        self.assertEqual(calc(110000, 99000, Settings(tax_mode="special")).consumption_tax, 2000)

    def test_points_and_thresholds(self):
        s = Settings(extra_point_rate=0.05, point_value=0.5, min_profit_yen=1000, min_profit_rate=0.02)
        b = calc(100000, 110000, s, api_points=1000)
        # 追加ポイント: 税抜100,000円 × 5% = 5,000pt → 合計6,000pt × 0.5円
        self.assertEqual((b.points, b.point_yen), (6000, 3000))
        self.assertEqual(judge(b, s), JUDGE_LOSS)
        self.assertEqual(judge(calc(100000, 99500, Settings()), Settings()), JUDGE_THIN)
        self.assertEqual(judge(calc(100000, 99500, s, api_points=4000), Settings(judge_with_points=False)), JUDGE_THIN)

    def test_evaluate_picks_highest_profit_after_points_and_shipping(self):
        item = BuybackItem(site="SY", name="Canon EOS R8 ボディ", price=200000)
        offers = [
            Offer(name="キヤノン EOS R8 ボディ", price=190000, shipping_free=False),
            Offer(name="キヤノン EOS R8 ボディ", price=191000, points=9000, shipping_free=True, store="S2"),
            Offer(name="キヤノン EOS R8 ボディ 中古", price=150000, shipping_free=True),
            Offer(name="キヤノン EOS R8 RF24-50 レンズキット", price=185000, shipping_free=True),
            Offer(name="EOS R8 用 液晶保護フィルム", price=1500, shipping_free=True),
        ]
        r = evaluate(item, offers, Settings(), by_jan=False)
        self.assertEqual(r.offer.store, "S2")
        self.assertEqual((r.candidates, r.excluded), (2, 3))
        self.assertEqual(r.breakdown.profit, 200000 - 191000 + 9000)
        self.assertEqual(r.judge, JUDGE_GOOD)

    def test_jan_offer_cautions(self):
        item = BuybackItem(site="SY", name="EOS R8", price=200000, jan="4549292204605")
        r = evaluate(item, [Offer(name="EOS R8 セット", price=100000, store_reviews=3, shipping_free=None)],
                     Settings(), by_jan=True)
        text = " ".join(r.cautions)
        for word in ("セット", "送料不明", "件数が少ない", "極端に安い"):
            self.assertIn(word, text)

    def test_check_item_falls_back_to_name(self):
        item = BuybackItem(site="SY", name="Nikon Z6III ボディ", price=268000, jan="4960759916406")
        calls = []

        def fake_search(**kw):
            calls.append(kw)
            if "jan" in kw:
                return []
            return [Offer(name="ニコン Z 6III ボディ", price=259800, points=2361, shipping_free=True)]

        r = check_item(item, Settings(), fake_search)
        self.assertEqual(r.match, MATCH_NAME)
        self.assertEqual(calls[1]["price_from"], int(268000 * 0.6))
        self.assertEqual(r.judge, JUDGE_GOOD)

        r = check_item(BuybackItem(site="SY", name="カメラ 福袋", price=10000), Settings(), fake_search)
        self.assertEqual(r.judge, JUDGE_NONE)
        self.assertIn("型番がない", r.note)

    def test_check_item_jan_hit(self):
        item = BuybackItem(site="SY", name="EOS R8", price=200000, jan="4549292204605")
        r = check_item(item, Settings(), lambda **kw: [Offer(name="EOS R8", price=210000, shipping_free=True)])
        self.assertEqual((r.match, r.judge), (MATCH_JAN, JUDGE_LOSS))


class ReportTest(unittest.TestCase):
    def results(self):
        s = Settings()
        good = evaluate(BuybackItem(site="SY", name="<b>EOS R8</b>", price=200000, url="javascript:alert(1)"),
                        [Offer(name="EOS R8", price=190000, shipping_free=True, url="https://x/\"><script>")],
                        s, by_jan=True)
        loss = evaluate(BuybackItem(site="SY", name="Z8", price=400000, jan="4960759910381"),
                        [Offer(name="Z8", price=420000, shipping_free=True)], s, by_jan=True)
        none = check_item(BuybackItem(site="SY", name="福袋", price=1000), s, lambda **kw: [])
        return s, [loss, none, good]

    def test_console_and_csv(self):
        s, results = self.results()
        text = render_console(results, s)
        self.assertIn("3商品", text)
        self.assertIn("<b>EOS R8</b>", text)
        self.assertNotIn("Z8 ", text.split("\n\n")[1])  # 赤字は既定では表示しない
        lines = render_csv(results).splitlines()
        self.assertTrue(lines[1].startswith(JUDGE_GOOD))
        self.assertEqual(len(lines), 4)

    def test_html_escapes(self):
        s, results = self.results()
        page = render_html(results, s, datetime(2026, 10, 18, 9, 0))
        self.assertNotIn("<b>EOS", page)
        self.assertNotIn("<script>", page)
        self.assertNotIn('href="javascript', page)
        self.assertIn("&lt;b&gt;EOS R8&lt;/b&gt;", page)


class CliTest(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_calc(self):
        code, out, _ = self.run_cli(["calc", "--sell", "105000", "--buy", "98000", "--points", "890"])
        self.assertEqual(code, 0)
        self.assertIn("+7,890", out)
        self.assertIn(JUDGE_GOOD, out)

    def test_fetch_html_then_check(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            page = d / "page.html"
            page.write_text(CARDS_HTML, encoding="utf-8")
            listing = d / "list.csv"
            code, out, err = self.run_cli(["fetch", "--html", str(page), "--site", "SY", "--url", BASE,
                                           "-o", str(listing)])
            self.assertEqual(code, 0, err)
            self.assertIn("3件を保存", out)

            def fake_search(appid, **kw):
                self.assertEqual(appid, "test-id")
                if kw.get("jan") == "4960759916406":
                    return [Offer(name="ニコン Z6III ボディ", price=255000, points=2300, shipping_free=True,
                                  store="カメラ屋")]
                return []

            config = d / "settings.json"
            config.write_text(json.dumps({"yahoo_appid": "test-id", "yahoo_interval_sec": 0.5}), encoding="utf-8")
            with mock.patch.object(cli, "search", fake_search), mock.patch.object(cli.time, "sleep"):
                code, out, err = self.run_cli(["check", "-c", str(config), "-i", str(listing),
                                               "-o", str(d / "reports")])
            self.assertEqual(code, 0, err)
            self.assertIn("Nikon Z6III", out)
            self.assertIn("+15,300", out)
            self.assertEqual(len(list((d / "reports").glob("kaitori_*.csv"))), 1)
            self.assertEqual(len(list((d / "reports").glob("kaitori_*.html"))), 1)

    def test_check_requires_appid(self):
        with tempfile.TemporaryDirectory() as d:
            config = Path(d) / "s.json"
            config.write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"YAHOO_APPID": ""}):
                with self.assertRaises(SystemExit) as cm:
                    self.run_cli(["check", "-c", str(config)])
            self.assertIn("Client ID", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
