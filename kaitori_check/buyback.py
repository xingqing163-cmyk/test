"""買取サイトの価格一覧の取得と読み書き。

サイトごとにページの作りが違うので、次の3つの読み方を試し、いちばん多く商品が
取れた結果を使う。
  1. JSON-LD（検索エンジン向けの商品情報）
  2. ページに埋め込まれたJSON（Next.js の __NEXT_DATA__ や self.__next_f など）
  3. HTMLの「同じ形の枠が繰り返し並んでいる部分」を商品カードとみなして読む
どれでも取れない（ブラウザでJavaScriptを動かさないと表示されない）サイトは、
ブラウザで表示したページを保存して --html で渡すか、CSVを手で作って使う。
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

USER_AGENT = "kaitori-check/0.1 (personal use)"

Fetcher = Callable[[str], bytes]


def http_get(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en;q=0.5",
    })
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


@dataclass
class BuybackItem:
    """買取サイトの1商品。price は新品（未開封）の買取価格（円・税込）。"""

    site: str
    name: str
    price: int
    category: str = ""
    jan: str = ""
    condition: str = ""
    url: str = ""
    fetched_at: str = ""

    @property
    def key(self) -> Tuple[str, str, str, int]:
        return (self.site, self.jan or "", re.sub(r"\s+", "", self.name), self.price)


# ---------------------------------------------------------------- 価格・JANの読み取り

_PRICE_RE = re.compile(r"[¥￥]\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)|([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\s*円")
JAN_RE = re.compile(r"(?<![0-9])(4[59][0-9]{11})(?![0-9])")
_NEW_RE = re.compile(r"新品|未開封|未使用")
_NOT_NEW_RE = re.compile(r"開封済|開封品|中古|使用品")
# 金額の直前・直後がこの言葉なら買取価格ではない（定価・送料・「〇円以上」など）
_NOT_BUYBACK_BEFORE_RE = re.compile(r"定価|参考価格|販売価格|希望小売|送料|手数料|減額|ポイント|クーポン")
# 区分や操作のラベル（「新品・未開封」「買取価格(税込)」「カートに入れる」など）は商品名ではない
_LABEL_WORD = (r"新品|未開封|開封済み|開封済|開封|未使用|使用品|中古|美品|良品|並品|難あり|箱なし|箱あり|付属品|"
               r"ランク|[SABC]|買取価格|買取|価格|上限|最大|税込|税抜|JAN|在庫|あり|なし|詳細|もっと見る|"
               r"カートに入れる|カートへ|追加|申し込む|申込|お問い合わせ|問合せ|要問合せ")
_LABEL_ONLY_RE = re.compile(rf"[\s・/／|｜()（）:：\-]*(?:(?:{_LABEL_WORD})[\s・/／|｜()（）:：\-]*)+")
_NOT_BUYBACK_AFTER_RE = re.compile(r"\s*(?:以上|未満|まで|から|〜|～|引|割引|off|OFF|pt|ポイント)")
MIN_PRICE, MAX_PRICE = 100, 20_000_000


def yen_to_int(text: Any) -> Optional[int]:
    """「¥123,000」「123000円」「123,000」→ 123000。読めなければ None。"""
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return int(text)
    s = unicodedata.normalize("NFKC", str(text or "")).replace(",", "").replace("¥", "")
    s = s.replace("円", "").strip()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", s):
        return None
    return int(float(s))


def is_new_label(label: str) -> bool:
    return bool(_NEW_RE.search(label)) and not _NOT_NEW_RE.search(label.replace("未開封", ""))


def price_candidates(text: str) -> List[Tuple[str, int]]:
    """文章中の金額と、その直前の言葉（区分のラベル）の組を取り出す。"""
    out = []
    t = unicodedata.normalize("NFKC", text)
    for m in _PRICE_RE.finditer(t):
        v = yen_to_int(m.group(1) or m.group(2))
        if v is None or not MIN_PRICE <= v <= MAX_PRICE:
            continue
        before = t[max(0, m.start() - 16):m.start()]
        after = t[m.end():m.end() + 6]
        # 直前の金額より後ろだけをラベルとする（「新品 ¥10,000 中古 ¥8,000」の中古に新品が混ざらないように）
        prev = list(_PRICE_RE.finditer(before))
        if prev:
            before = before[prev[-1].end():]
        if _NOT_BUYBACK_BEFORE_RE.search(before[-8:]) or _NOT_BUYBACK_AFTER_RE.match(after):
            continue
        out.append((_label_suffix(before), v))
    return out


def _label_suffix(before: str) -> str:
    """金額の直前の文字列から、区分のラベル部分（「新品・未開封」など）だけを取り出す。"""
    b = before.rstrip(" :：/|-")
    for i in range(len(b)):
        if _LABEL_ONLY_RE.fullmatch(b[i:]):
            return b[i:].strip(" :：/|-・")
    return b[-8:].strip()


def pick_price(cands: Sequence[Tuple[str, int]]) -> Optional[Tuple[int, str]]:
    """新品（未開封）の価格を優先し、なければいちばん高い価格を選ぶ。"""
    if not cands:
        return None
    new = [c for c in cands if is_new_label(c[0])]
    label, price = max(new or cands, key=lambda c: c[1])
    return price, label


# ---------------------------------------------------------------- 1. JSON-LD

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)


def _scripts(html: str) -> List[Tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in _SCRIPT_RE.finditer(html)]


def _iter_dicts(obj: Any, depth: int = 0) -> Iterable[Dict[str, Any]]:
    if depth > 80:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dicts(v, depth + 1)


def parse_jsonld(html: str, site: str, base_url: str) -> List[BuybackItem]:
    items = []
    for attrs, body in _scripts(html):
        if "ld+json" not in attrs.lower():
            continue
        try:
            data = json.loads(body.strip())
        except ValueError:
            continue
        for d in _iter_dicts(data):
            types = d.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Product" not in types or not isinstance(d.get("name"), str):
                continue
            offers = d.get("offers")
            offers = offers if isinstance(offers, list) else [offers]
            cands = []
            for o in offers:
                if not isinstance(o, dict):
                    continue
                for k in ("price", "highPrice", "lowPrice"):
                    v = yen_to_int(o.get(k))
                    if v is not None and MIN_PRICE <= v <= MAX_PRICE:
                        cands.append((str(o.get("name") or o.get("itemCondition") or ""), v))
            picked = pick_price(cands)
            if not picked:
                continue
            jan = next((str(d[k]) for k in ("gtin13", "gtin", "ean") if JAN_RE.fullmatch(str(d.get(k, "")))), "")
            url = d.get("url") if isinstance(d.get("url"), str) else ""
            items.append(BuybackItem(site=site, name=_clean_name(d["name"]), price=picked[0], jan=jan,
                                     condition=picked[1], url=urllib.parse.urljoin(base_url, url)))
    return items


# ---------------------------------------------------------------- 2. 埋め込みJSON

_NAME_KEYS = ("name", "title", "productname", "product_name", "itemname", "item_name", "displayname",
              "display_name", "modelname", "model_name", "goodsname", "goods_name")
_JAN_KEYS = ("jan", "jancode", "jan_code", "ean", "gtin", "gtin13", "barcode")
_URL_KEYS = ("url", "href", "link", "path")
# 定価・販売価格など、買取価格ではない値の名前
_NOT_BUYBACK_KEY_RE = re.compile(r"list|retail|msrp|original|regular|teika|sell|sale|shipping|fee|point", re.I)
_LABEL_KEYS = ("condition", "conditionname", "condition_name", "rank", "grade", "status", "label", "type",
               "name", "title")
_NEXT_F_RE = re.compile(r"self\.__next_f\.push\(\[\s*1\s*,\s*(\"(?:[^\"\\]|\\.)*\")\s*\]\)", re.S)


def _lower_keys(d: Dict[str, Any]) -> Dict[str, Any]:
    return {str(k).lower(): v for k, v in d.items()}


def _price_value(key: str, v: Any) -> Optional[int]:
    if "price" not in key and key not in ("kaitori", "buyback", "amount"):
        return None
    if _NOT_BUYBACK_KEY_RE.search(key):
        return None
    p = yen_to_int(v)
    return p if p is not None and MIN_PRICE <= p <= MAX_PRICE else None


def _label_of(d: Dict[str, Any]) -> str:
    low = _lower_keys(d)
    return " ".join(str(low[k]) for k in _LABEL_KEYS if isinstance(low.get(k), str))


def _product_from_dict(d: Dict[str, Any], site: str, base_url: str) -> Optional[BuybackItem]:
    low = _lower_keys(d)
    name = next((low[k] for k in _NAME_KEYS if isinstance(low.get(k), str) and len(low[k].strip()) >= 3), None)
    # 「新品未開封」のような区分のラベルは商品名ではない（価格の内訳の要素）
    if name is None or _LABEL_ONLY_RE.fullmatch(unicodedata.normalize("NFKC", name)):
        return None
    cands: List[Tuple[str, int]] = []
    for k, v in low.items():
        p = _price_value(k, v)
        if p is not None:
            cands.append((k, p))
        elif isinstance(v, dict) and ("price" in k or "kaitori" in k):
            # "prices": {"new": 1000, "used": 800} の形
            for k2, v2 in v.items():
                p2 = yen_to_int(v2) if not _NOT_BUYBACK_KEY_RE.search(str(k2)) else None
                if p2 is not None and MIN_PRICE <= p2 <= MAX_PRICE:
                    cands.append((f"{k2}", p2))
        elif isinstance(v, list) and ("price" in k or "kaitori" in k or "variant" in k or "condition" in k):
            # "prices": [{"condition": "新品", "price": 1000}, ...] の形
            for e in v:
                if not isinstance(e, dict):
                    continue
                for k2, v2 in _lower_keys(e).items():
                    p2 = _price_value(k2, v2)
                    if p2 is not None:
                        cands.append((_label_of(e) or k2, p2))
    picked = pick_price([(_json_label(l), p) for l, p in cands])
    if not picked:
        return None
    jan = ""
    for k in _JAN_KEYS:
        v = str(low.get(k) or "")
        if JAN_RE.fullmatch(v):
            jan = v
            break
    if not jan:
        m = JAN_RE.search(name)
        jan = m.group(1) if m else ""
    url = next((low[k] for k in _URL_KEYS if isinstance(low.get(k), str) and low[k].startswith(("/", "http"))), "")
    return BuybackItem(site=site, name=_clean_name(name), price=picked[0], jan=jan, condition=picked[1],
                       url=urllib.parse.urljoin(base_url, url) if url else "")


def _json_label(key: str) -> str:
    """JSONのキー名を区分のラベルに読み替える（newPrice → 新品）。"""
    k = key.lower()
    if re.search(r"new|unopen|mikaifu|shinpin|sealed", k):
        return f"新品 {key}"
    if re.search(r"used|open|chuko", k):
        return f"中古 {key}"
    return key


def _walk_products(obj: Any, site: str, base_url: str, out: List[BuybackItem], depth: int = 0) -> None:
    if depth > 80:
        return
    if isinstance(obj, dict):
        item = _product_from_dict(obj, site, base_url)
        if item is not None and not _holds_products(obj, site, base_url):
            out.append(item)
            return
        for v in obj.values():
            _walk_products(v, site, base_url, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _walk_products(v, site, base_url, out, depth + 1)


def _holds_products(d: Dict[str, Any], site: str, base_url: str) -> bool:
    """商品の一覧を中に持つ入れ物か（例: 最低価格つきのカテゴリ {"name", "minPrice", "products": [...]}）。"""
    for v in d.values():
        if isinstance(v, list) and sum(
                1 for e in v if isinstance(e, dict) and _product_from_dict(e, site, base_url)) >= 2:
            return True
    return False


def _embedded_json(html: str) -> List[Any]:
    blobs: List[Any] = []
    for attrs, body in _scripts(html):
        a = attrs.lower()
        if "application/json" in a or "__next_data__" in a:
            try:
                blobs.append(json.loads(body.strip()))
            except ValueError:
                pass
    # Next.js (App Router) のデータ: self.__next_f.push([1,"..."]) をつなげ、行ごとにJSONとして読む
    flight = []
    for m in _NEXT_F_RE.finditer(html):
        try:
            flight.append(json.loads(m.group(1)))
        except ValueError:
            pass
    for line in "".join(flight).splitlines():
        m = re.match(r"^[0-9a-f]+:([\[{\"].*)$", line)
        if not m:
            continue
        try:
            blobs.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return blobs


def parse_embedded(html: str, site: str, base_url: str) -> List[BuybackItem]:
    items: List[BuybackItem] = []
    for blob in _embedded_json(html):
        _walk_products(blob, site, base_url, items)
    return items


# ---------------------------------------------------------------- 3. HTMLの商品カード

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
         "track", "wbr"}
_SKIP_TEXT = {"script", "style", "noscript", "template", "svg"}


class _Node:
    __slots__ = ("tag", "attrs", "children", "parent", "_chunks")

    def __init__(self, tag: str, attrs: Dict[str, str], parent: Optional["_Node"]):
        self.tag = tag
        self.attrs = attrs
        self.children: List[Any] = []
        self.parent = parent
        self._chunks: Optional[List[str]] = None

    def elements(self) -> Iterable["_Node"]:
        stack = [self]
        while stack:
            n = stack.pop()
            yield n
            stack.extend(reversed([c for c in n.children if isinstance(c, _Node)]))

    def chunks(self) -> List[str]:
        """この要素の中の文字列（空白を詰めたもの）の一覧。"""
        if self._chunks is None:
            out: List[str] = []
            if self.tag not in _SKIP_TEXT:
                for c in self.children:
                    if isinstance(c, _Node):
                        out.extend(c.chunks())
                    else:
                        s = re.sub(r"\s+", " ", c).strip()
                        if s:
                            out.append(s)
            self._chunks = out
        return self._chunks

    def text(self) -> str:
        return " ".join(self.chunks())

    def signature(self) -> Tuple[str, str]:
        return (self.tag, " ".join(sorted(self.attrs.get("class", "").split())))



class _DomBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        node = _Node(tag, {k: v or "" for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in _VOID:
            self.cur = node

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.cur.children.append(_Node(tag, {k: v or "" for k, v in attrs}, self.cur))

    def handle_endtag(self, tag: str) -> None:
        n: Optional[_Node] = self.cur
        while n is not None and n is not self.root and n.tag != tag:
            n = n.parent
        if n is not None and n is not self.root:
            self.cur = n.parent or self.root

    def handle_data(self, data: str) -> None:
        self.cur.children.append(data)


def _is_name_chunk(s: str) -> bool:
    if len(s) < 3 or _PRICE_RE.search(s) or JAN_RE.fullmatch(s.replace("JAN", "").strip(" :：")):
        return False
    if re.fullmatch(r"[\s\d,.:：/|()（）\-¥￥円%]+", s):
        return False
    return not _LABEL_ONLY_RE.fullmatch(unicodedata.normalize("NFKC", s))


def _card_name(card: _Node) -> str:
    for n in card.elements():
        if n is card:
            continue
        if n.tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            t = " ".join(c for c in n.chunks() if _is_name_chunk(c))
            if t:
                return t
    for n in card.elements():
        cls = n.attrs.get("class", "").lower()
        if n is not card and re.search(r"name|title", cls):
            t = " ".join(c for c in n.chunks() if _is_name_chunk(c))
            if t:
                return t
    names = [c for c in card.chunks() if _is_name_chunk(c)]
    return max(names, key=len) if names else ""


def _card_url(card: _Node, base_url: str) -> str:
    for n in card.elements():
        href = n.attrs.get("href", "")
        if n.tag == "a" and href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return urllib.parse.urljoin(base_url, href)
    return ""


def _card_jan(card: _Node) -> str:
    for n in card.elements():
        for k, v in n.attrs.items():
            if "jan" in k.lower() and JAN_RE.fullmatch(v.strip()):
                return v.strip()
    m = JAN_RE.search(card.text())
    return m.group(1) if m else ""


def _has_ancestor_in(node: _Node, ids: set) -> bool:
    n = node.parent
    while n is not None:
        if id(n) in ids:
            return True
        n = n.parent
    return False


def parse_cards(html: str, site: str, base_url: str) -> List[BuybackItem]:
    builder = _DomBuilder()
    builder.feed(html)
    builder.close()
    groups: Dict[Tuple[str, str], List[_Node]] = {}
    for n in builder.root.elements():
        if n.tag in ("#root", "html", "head", "body", "main") or n.tag in _SKIP_TEXT:
            continue
        text = n.text()
        if not 4 <= len(text) <= 600:
            continue
        cands = price_candidates(text)
        if not 1 <= len(cands) <= 6 or not any(_is_name_chunk(c) for c in n.chunks()):
            continue
        groups.setdefault(n.signature(), []).append(n)
    if not groups:
        return []
    # いちばん多く繰り返されている形を商品カードとみなす（同数なら中身の多い外側の枠）
    best = max(groups.values(), key=lambda g: (len(g), sum(len(n.text()) for n in g) / len(g)))
    # 入れ子になっている場合は外側の枠だけ残す
    ids = {id(n) for n in best}
    cards = [n for n in best if not _has_ancestor_in(n, ids)]
    items = []
    for card in cards:
        picked = pick_price(price_candidates(card.text()))
        name = _clean_name(_card_name(card))
        if not picked or not name:
            continue
        items.append(BuybackItem(site=site, name=name, price=picked[0], jan=_card_jan(card),
                                 condition=picked[1], url=_card_url(card, base_url)))
    return items


# ---------------------------------------------------------------- まとめ

PARSERS = (("JSON-LD", parse_jsonld), ("埋め込みJSON", parse_embedded), ("HTMLの商品カード", parse_cards))


def _clean_name(name: str) -> str:
    s = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", name)).strip()
    s = re.sub(r"\s*JAN\s*[:：]?\s*4[59][0-9]{11}", "", s)
    return s[:200]


def dedupe(items: Iterable[BuybackItem]) -> List[BuybackItem]:
    seen = set()
    out = []
    for it in items:
        if it.key not in seen:
            seen.add(it.key)
            out.append(it)
    return out


def parse_listing(html: str, site: str, base_url: str) -> Tuple[List[BuybackItem], str]:
    """3つの読み方を試し、いちばん多く商品が取れた結果と読み方の名前を返す。"""
    best: Tuple[List[BuybackItem], str] = ([], "")
    for label, parser in PARSERS:
        try:
            items = dedupe(parser(html, site, base_url))
        except (ValueError, RecursionError, AssertionError):
            continue
        if len(items) > len(best[0]):
            best = (items, label)
    return best


def decode_html(raw: bytes) -> str:
    """UTF-8 で読めなければ meta の charset（なければ Shift_JIS）で読む。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    m = re.search(rb"charset=[\"']?([A-Za-z0-9_\-]+)", raw[:4096])
    enc = m.group(1).decode("ascii") if m else "cp932"
    if enc.lower().replace("-", "_") in ("shift_jis", "sjis", "x_sjis"):
        enc = "cp932"
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("cp932", errors="replace")


def page_links(html: str, base_url: str) -> Dict[int, str]:
    """同じ一覧ページの「2ページ目」「3ページ目」…へのリンクを集める。"""
    base = urllib.parse.urlsplit(base_url)
    out: Dict[int, str] = {}
    for m in re.finditer(r"href=[\"']([^\"']+)[\"']", html):
        href = urllib.parse.urljoin(base_url, m.group(1).replace("&amp;", "&"))
        u = urllib.parse.urlsplit(href)
        if u.netloc != base.netloc or u.path != base.path:
            continue
        q = urllib.parse.parse_qs(u.query)
        for k in ("page", "p", "pg"):
            v = q.get(k, [""])[0]
            if v.isdigit():
                out.setdefault(int(v), href)
    return out


def with_page(url: str, n: int, param: str = "page") -> str:
    u = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if k != param]
    q.append((param, str(n)))
    return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q)))


def robots_allows(url: str, fetch: Fetcher = http_get) -> bool:
    """robots.txt でこのツールの取得が禁止されていないか。確認できなければ許可とみなす。"""
    u = urllib.parse.urlsplit(url)
    robots_url = f"{u.scheme}://{u.netloc}/robots.txt"
    try:
        text = decode_html(fetch(robots_url))
    except urllib.error.HTTPError as e:
        return e.code not in (401, 403)
    except (urllib.error.URLError, OSError, ValueError):
        return True
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(text.splitlines())
    return rp.can_fetch(USER_AGENT, url)


@dataclass
class FetchLog:
    url: str
    count: int
    new: int
    parser: str


def fetch_site(site_name: str, url: str, category: str = "", *, fetch: Fetcher = http_get,
               max_pages: int = 30, interval: float = 2.0,
               on_page: Optional[Callable[[FetchLog], None]] = None,
               save_raw: Optional[Callable[[int, str], None]] = None) -> List[BuybackItem]:
    """一覧の1ページ目から順に取得し、新しい商品が出てこなくなったら止める。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    items: List[BuybackItem] = []
    seen = set()
    links: Dict[int, str] = {}
    param = "page"
    page_url = url
    for n in range(1, max_pages + 1):
        if n > 1:
            time.sleep(interval)
            page_url = links.get(n) or with_page(url, n, param)
            try:
                html = decode_html(fetch(page_url))
            except (urllib.error.URLError, OSError) as e:
                # 2ページ目以降の失敗（ページがない等）は、そこまでの結果を残して終える
                if on_page:
                    on_page(FetchLog(page_url, 0, 0, f"取得できず（{e}）"))
                break
        else:
            html = decode_html(fetch(page_url))
        if save_raw:
            save_raw(n, html)
        parsed, parser = parse_listing(html, site_name, page_url)
        new = [i for i in parsed if i.key not in seen]
        if on_page:
            on_page(FetchLog(page_url, len(parsed), len(new), parser))
        for it in new:
            seen.add(it.key)
            it.category = it.category or category
            it.fetched_at = now
            items.append(it)
        if not new:
            break
        found = page_links(html, page_url)
        links.update({k: v for k, v in found.items() if k not in links})
        if found and n == 1:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(next(iter(found.values()))).query)
            param = next((k for k in ("page", "p", "pg") if k in q), "page")
    return items


# ---------------------------------------------------------------- CSV

CSV_HEADERS = ["サイト", "カテゴリ", "商品名", "JAN", "買取価格", "区分", "URL", "取得日時"]
_ALIASES = {
    "サイト": "site", "site": "site", "買取サイト": "site",
    "カテゴリ": "category", "category": "category",
    "商品名": "name", "name": "name", "品名": "name",
    "jan": "jan", "janコード": "jan", "jan_code": "jan",
    "買取価格": "price", "price": "price", "価格": "price", "新品買取価格": "price",
    "区分": "condition", "condition": "condition", "状態": "condition",
    "url": "url", "取得日時": "fetched_at", "fetched_at": "fetched_at",
}


def excel_safe(v: Any) -> str:
    """Excel で数式として実行されないよう、= + - @ で始まる文字列の先頭に ' を付ける。"""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") and not re.fullmatch(r"-?[0-9.,]+", s) else s


def _unsafe(s: str) -> str:
    return s[1:] if s[:2] in ("'=", "'+", "'-", "'@") else s


def items_to_csv(items: Iterable[BuybackItem]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADERS)
    for it in items:
        w.writerow([excel_safe(x) for x in (it.site, it.category, it.name, it.jan, it.price, it.condition,
                                            it.url, it.fetched_at)])
    return buf.getvalue()


def parse_items_csv(text: str) -> List[BuybackItem]:
    lines = [l for l in text.lstrip("﻿").splitlines() if l.strip() and not l.lstrip().startswith("#")]
    reader = csv.reader(lines)
    header = next(reader, None)
    if header is None:
        return []
    cols = [_ALIASES.get(unicodedata.normalize("NFKC", h).strip().lower(), "") for h in header]
    if "name" not in cols or "price" not in cols:
        raise ValueError("買取価格リストのCSVには「商品名」「買取価格」の列が必要です")
    items = []
    for lineno, row in enumerate(reader, start=2):
        d = {c: _unsafe(v.strip()) for c, v in zip(cols, row) if c}
        price = yen_to_int(d.get("price"))
        if not d.get("name") or price is None:
            raise ValueError(f"買取価格リストの{lineno}行目: 商品名と買取価格（数字）を入れてください")
        jan = re.sub(r"[^0-9]", "", unicodedata.normalize("NFKC", d.get("jan", "")))
        items.append(BuybackItem(site=d.get("site", ""), name=d["name"], price=price,
                                 category=d.get("category", ""), jan=jan,
                                 condition=d.get("condition", ""), url=d.get("url", ""),
                                 fetched_at=d.get("fetched_at", "")))
    return items
