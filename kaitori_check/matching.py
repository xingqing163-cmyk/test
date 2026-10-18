"""買取商品とヤフショ商品の突き合わせ（型番の比較・除外語・キット違いの判定）。

JANコードで見つかった商品はそのまま同一商品とみなす。名前で探した商品は、
買取側の商品名から取り出した型番（英数字の語）がすべてヤフショの商品名に含まれ、
世代違い（Mark II / III、GM II など）やキット違いがないものだけを候補にする。
名前一致はあくまで推定なので、レポートでは「要確認」として扱う。
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Optional, Sequence, Set

# 型番の比較で無視する語（メーカー名・一般名詞・色）
STOP_TOKENS = {
    "canon", "nikon", "sony", "fujifilm", "fuji", "panasonic", "lumix", "olympus", "om", "system",
    "omsystem", "sigma", "tamron", "tokina", "ricoh", "pentax", "leica", "hasselblad", "zeiss",
    "samyang", "viltrox", "laowa", "voigtlander", "cosina", "kenko", "dji", "gopro", "insta360",
    "body", "kit", "lens", "lenses", "camera", "digital", "mirrorless", "new", "jan", "only",
    "black", "silver", "white", "gray", "grey", "brown", "red", "blue", "green", "bk", "wh",
    "the", "and", "with", "for", "mm",
}
# 世代を表す語（買取側にないのにヤフショ側にあれば別モデル）
GENERATION_TOKENS = {"ii", "iii", "iv", "v", "vi", "vii", "viii", "mk2", "mk3", "mk4", "mk5",
                     "mk6", "mk7", "mk8"}
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}

KIT_WORDS = ("キット", "kit", "レンズ付", "レンズセット")
BODY_WORDS = ("ボディ", "body", "本体のみ")
COLOR_GROUPS = (("ブラック", "black"), ("シルバー", "silver"), ("ホワイト", "white"),
                ("グレー", "gray", "grey"), ("ブラウン", "brown"), ("レッド", "red"), ("ブルー", "blue"),
                ("グリーン", "green"))

# 検索語から外す飾り（【新品】・新品未開封 など）
_DECORATION_RE = re.compile(
    r"【[^】]*】|\[[^\]]*(?:新品|未開封|中古|買取)[^\]]*\]|"
    r"新品未開封|新品|未開封|未使用|国内正規品|正規品|買取価格|買取"
)
_MARK_RE = re.compile(r"\bm(?:ar)?k\.?\s*-?\s*(viii|vii|vi|iv|v|iii|ii|i|x|ix|\d{1,2})\b")
_TOKEN_RE = re.compile(r"[0-9a-zα-ωµμ][0-9a-zα-ωµμ.\-]*")


def normalize(text: str) -> str:
    """全角→半角・小文字化（Ⅱ→ii、ＥＯＳ→eos など）。"""
    return unicodedata.normalize("NFKC", text or "").lower()


def contains_any(text: str, words: Sequence[str]) -> List[str]:
    t = normalize(text)
    return [w for w in words if w and normalize(w) in t]


def clean_query(name: str) -> str:
    """ヤフショの検索語に使う商品名（飾りを除き、長すぎれば切る）。"""
    q = _DECORATION_RE.sub(" ", unicodedata.normalize("NFKC", name or ""))
    q = re.sub(r"[()（）「」『』]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q[:80]


def _mark(m: "re.Match[str]") -> str:
    g = m.group(1)
    n = int(g) if g.isdigit() else _ROMAN.get(g, 0)
    return f" mk{n} " if n else m.group(0)


def raw_tokens(name: str) -> List[str]:
    """商品名から英数字の語を順番に取り出す（ハイフンは除き、Mark II は mk2 にそろえる）。"""
    t = normalize(name).replace("f/", "f")
    t = _MARK_RE.sub(_mark, t)
    out = []
    for tok in _TOKEN_RE.findall(t):
        tok = tok.replace("-", "").strip(".")
        if tok:
            out.append(tok)
    return out


def token_set(name: str) -> Set[str]:
    """比較用の語の集合。隣り合う2語をつなげた語も含める（「Z 6III」と「Z6III」を同じに扱う）。"""
    toks = raw_tokens(name)
    out = set(toks)
    for a, b in zip(toks, toks[1:]):
        out.add(a + b)
    return out


def model_tokens(name: str) -> List[str]:
    """型番の比較に使う語（メーカー名・色などを除く）。"""
    return [t for t in raw_tokens(name) if t not in STOP_TOKENS]


def has_model_number(name: str) -> bool:
    """数字を含む語（型番らしきもの）があるか。なければ名前での照合は危険なので行わない。"""
    return any(any(c.isdigit() for c in t) for t in model_tokens(name))


def name_matches(buyback_name: str, offer_name: str) -> bool:
    """買取側の型番がすべてヤフショ側に含まれ、世代違いでなければ True。"""
    need = model_tokens(buyback_name)
    if not need or not has_model_number(buyback_name):
        return False
    have = token_set(offer_name)
    i = 0
    while i < len(need):
        if need[i] in have:
            i += 1
        elif i + 1 < len(need) and need[i] + need[i + 1] in have:
            i += 2
        else:
            return False
    # ヤフショ側にだけ世代を表す語があれば別モデル（例: 買取「α7」とヤフショ「α7 IV」）
    mine = token_set(buyback_name)
    for g in GENERATION_TOKENS & set(raw_tokens(offer_name)):
        if not any(t == g or t.endswith(g) for t in mine):
            return False
    return True


def kit_mismatch(buyback_name: str, offer_name: str) -> Optional[str]:
    """ボディ単体とレンズキットの取り違えがあれば理由を返す。"""
    b_kit = bool(contains_any(buyback_name, KIT_WORDS))
    o_kit = bool(contains_any(offer_name, KIT_WORDS))
    if b_kit and not o_kit:
        return "買取はキット、ヤフショはキットでない"
    if o_kit and not b_kit:
        return "ヤフショはキット、買取はキットでない"
    return None


def color_conflict(buyback_name: str, offer_name: str) -> bool:
    """買取側とヤフショ側で、はっきり別の色が書かれていれば True（例: シルバーとブラック）。"""
    mine = [g for g in COLOR_GROUPS if _has_color(g, buyback_name)]
    theirs = [g for g in COLOR_GROUPS if _has_color(g, offer_name)]
    return bool(mine and theirs and not set(mine) & set(theirs))


def color_caution(buyback_name: str, offer_name: str) -> Optional[str]:
    """買取側に色の指定があるのにヤフショ側の名前にその色がなければ注意。"""
    for group in COLOR_GROUPS:
        if _has_color(group, buyback_name) and not _has_color(group, offer_name):
            return f"色違いの可能性（買取側: {group[0]}）"
    return None


def _has_color(group: Sequence[str], name: str) -> bool:
    # 英語の色名は語として（"red" が "reduce" に一致しないように）、カタカナは部分一致で探す
    tokens, text = set(raw_tokens(name)), normalize(name)
    return any(w in tokens if w.isascii() else w in text for w in group)
