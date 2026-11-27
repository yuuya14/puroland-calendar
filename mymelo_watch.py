#!/usr/bin/env python3
"""マイメロディの外部イベント（商業施設などでのグリーティング・ステージ）の情報を探して通知する。

  情報源 … Googleニュースの検索結果（RSS）と、Googleアラート（RSS。Secret「GOOGLE_ALERT_FEEDS」に登録した場合）
  絞り込み … キーワードのルールで「会えるイベント」らしいものだけ残す（AI判定はなし）
  重複 … 同じ記事・同じタイトルは一度しか通知しない（data/mymelo_seen.json に記録）
  出力 … 新しい候補があれば message_mymelo.txt（LINE通知用）を書き出す
          取得に失敗したら errors.md に追記する（GitHub Issue 用）

使い方: python3 mymelo_watch.py [--offline FILE ...]   # テスト用にRSSファイルを直接読む
"""
import argparse
import hashlib
import html
import json
import os
import re
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEEN = ROOT / "data" / "mymelo_seen.json"
MESSAGE_GLOB = "message_mymelo_*.txt"   # LINEの吹き出し1つ＝ファイル1つ（_1, _2, ...）
BUBBLE_CHARS = 4500                       # LINEの1吹き出しは最大5000文字。余裕を持たせる
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130 Safari/537.36")
GNEWS = "https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"
JST = timezone(timedelta(hours=9))

# ---- 絞り込みのルール ---------------------------------------------------------
CHARACTER = ("マイメロディ", "マイメロ", "MY MELODY", "MYMELODY")
# タイトルにあれば候補：グリーティングそのものを表す言葉
STRONG = ("グリーティング", "撮影会", "フォトセッション", "ハイタッチ", "握手", "ふれあい",
          "ステージ", "ショー", "に会える", "に会いに", "と会える")
# タイトルにあれば候補（ただしグッズ系の言葉が無いときだけ）
WEAK = ("会える", "会いに", "やってくる", "やって来る", "来店", "来場", "遊びに来る")
# 本文（Googleアラートの抜粋）だけで判定するときに使う、より確実な言葉
CORE = ("グリーティング", "撮影会", "ハイタッチ", "握手会", "ふれあい")
GOODS = ("グッズ", "フード", "メニュー", "スイーツ", "カフェ", "発売", "販売", "アイテム",
         "商品", "コラボ", "限定品", "プレゼント", "キャンペーン", "ボトル", "コスメ", "化粧",
         "雑貨", "ポップアップ", "ストア", "ショップ", "デザイン", "パッケージ", "新作", "予約",
         "パーカー", "アパレル", "ファッション", "ウェア", "カトラリー", "食器", "文具", "ステーショナリー",
         "絵本", "書籍", "雑誌", "付録", "おもちゃ", "玩具")
# 通知しないもの（ピューロランドは別の仕組みで見ている／明らかに会えるイベントではないもの）
EXCLUDE = ("ピューロランド", "ピューロ", "びょんわぁ", "プラモデル", "はんこ", "プライズ",
           "ホビーショー", "おもちゃショー", "CD", "アニメ", "映画", "ガチャ",
           "一番くじ", "ぬいぐるみ", "ゲーム", "アプリ")


def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html.unescape(s or ""))).strip()


def clean_title(title: str, source: str = "") -> str:
    """「画像8 / 27＞」「写真2/8｜」「(1/2)」などの付け足しや、末尾の「 - 媒体名」を取る。"""
    t = norm(title).strip()
    if source and t.endswith(" - " + norm(source)):
        t = t[: -len(" - " + norm(source))]
    t = re.sub(r"^[<＜]?\s*(画像|写真)\s*\d+\s*/\s*\d+\s*[>＞|｜]\s*", "", t)
    t = re.sub(r"(の画像|\(写真)\s*\d+\s*/\s*\d+\)?$", "", t)
    t = re.sub(r"[（(]\s*\d+\s*/\s*\d+\s*[)）]$", "", t)
    t = re.sub(r"[（(]写真\s*\d+\s*/\s*\d+[)）]", "", t)
    t = re.sub(r"^画像・写真\s*[|｜]\s*", "", t)
    t = re.sub(r"\s*\d+枚目(の写真・画像)?$", "", t)
    t = re.sub(r"\s+-\s+[^-]{1,40}$", "", t) if not source else t  # Googleアラートの「 - 媒体名」
    return re.sub(r"\s+", " ", t).strip()


def _has(t: str, words) -> bool:
    return any(norm(w) in t for w in words)


def is_candidate(title: str, summary: str = "") -> bool:
    """タイトル中心で判定する。抜粋（Googleアラート）は、マイメロの近くに確実な言葉があるときだけ使う。"""
    t, s = norm(title), norm(summary)
    if _has(t, EXCLUDE):
        return False
    if _has(t, CHARACTER):
        if _has(t, STRONG):
            return True
        return _has(t, WEAK) and not _has(t, GOODS)
    # タイトルにマイメロが無い記事：抜粋の中で「マイメロ」と「グリーティング」等が近く（40文字以内）にあること
    if _has(t, GOODS) or _has(s, EXCLUDE):
        return False
    pos_c = [m.start() for c in CHARACTER for m in re.finditer(re.escape(norm(c)), s)]
    pos_k = [m.start() for k in CORE for m in re.finditer(re.escape(norm(k)), s)]
    return any(abs(a - b) <= 40 for a in pos_c for b in pos_k)


# グッズ情報として通知しないもの（グッズではない話題）
EXCLUDE_GOODS = ("アニメ", "映画", "ゲーム", "アプリ", "びょんわぁ", "ショー", "ステージ")
GOODS_TITLE_HINT = ("サンリオ",)  # タイトルにマイメロが無くても、抜粋で判定してよい記事の目印


def classify(title: str, summary: str, goods_enabled: bool) -> str | None:
    """'event'（会えるイベント）/ 'goods'（グッズ情報）/ None（対象外）"""
    if is_candidate(title, summary):
        return "event"
    if not goods_enabled:
        return None
    t, s = norm(title), norm(summary)
    if _has(t, EXCLUDE_GOODS):
        return None
    if _has(t, CHARACTER):
        return "goods" if _has(t, GOODS) else None
    # タイトルが「サンリオキャラクターズの○○」型：抜粋でマイメロとグッズの言葉が近くにあれば対象
    if _has(t, GOODS_TITLE_HINT) and _has(t, GOODS):
        pos_c = [m.start() for c in CHARACTER for m in re.finditer(re.escape(norm(c)), s)]
        pos_g = [m.start() for g in GOODS for m in re.finditer(re.escape(norm(g)), s)]
        if any(abs(a - b) <= 40 for a in pos_c for b in pos_g):
            return "goods"
    return None


def key_of(title: str) -> str:
    t = re.sub(r"[\s!！?？♡♪☆★【】「」『』\[\]()（）・、。,.~〜ー\-]", "", norm(title)).lower()
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:16]


# ---- 取得 ---------------------------------------------------------------------
def http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_feed(text: str) -> list[dict]:
    """RSS 2.0（Googleニュース）と Atom（Googleアラート）の両方を読む。"""
    root = ET.fromstring(text.encode("utf-8"))
    items = []
    if root.tag.endswith("rss"):
        for it in root.iter("item"):
            source = (it.findtext("source") or "").strip()
            items.append({
                "title": clean_title(strip_tags(it.findtext("title") or ""), source),
                "summary": "", "link": (it.findtext("link") or "").strip(), "source": source,
                "date": _date(it.findtext("pubDate"))})
    else:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall("a:entry", ns):
            href = (e.find("a:link", ns).get("href") if e.find("a:link", ns) is not None else "")
            q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            link = q.get("url", [href])[0]
            items.append({
                "title": clean_title(strip_tags(e.findtext("a:title", "", ns))),
                "summary": strip_tags(e.findtext("a:content", "", ns)),
                "link": link, "source": urllib.parse.urlparse(link).netloc,
                "date": _date(e.findtext("a:published", "", ns) or e.findtext("a:updated", "", ns))})
    return items


def _date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---- main ---------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", nargs="*")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8")).get("external_events", {})
    if not cfg.get("enabled", True):
        return
    max_age = timedelta(days=cfg.get("max_age_days", 45))
    goods_enabled = bool(cfg.get("goods_enabled", False))
    goods_max_age = timedelta(days=cfg.get("goods_max_age_days", 7))
    now = datetime.now(timezone.utc)

    texts, errors = [], []
    if args.offline:
        texts = [(p, Path(p).read_text(encoding="utf-8")) for p in args.offline]
    else:
        queries = list(cfg.get("queries", []))
        if goods_enabled:
            queries += cfg.get("goods_queries", [])
        urls = [GNEWS.format(q=urllib.parse.quote(q)) for q in queries]
        urls += [u.strip() for u in re.split(r"[,\s]+", os.environ.get("GOOGLE_ALERT_FEEDS", "")) if u.strip()]
        for u in urls:
            try:
                texts.append((u, http_get(u)))
            except Exception as e:  # noqa: BLE001
                errors.append(f"- [マイメロ外部イベント] 取得できませんでした: {u[:80]}… ({e})")

    items = []
    for name, t in texts:
        try:
            items += parse_feed(t)
        except Exception as e:  # noqa: BLE001
            errors.append(f"- [マイメロ外部イベント] 読み取れませんでした: {name[:80]}… ({e})")

    first_run = not SEEN.exists()
    seen: dict = {} if first_run else json.loads(SEEN.read_text(encoding="utf-8"))

    today = now.astimezone(JST).date().isoformat()
    # グッズ情報を初めて有効にした回は、これまでの記事を記録だけして通知しない（大量に届かないように）
    goods_first = goods_enabled and "__goods_seeded__" not in seen
    new, goods, batch = [], [], set()
    for it in sorted(items, key=lambda x: x["date"] or now, reverse=True):
        if not it["title"]:
            continue
        kind = classify(it["title"], it["summary"], goods_enabled)
        if not kind:
            continue
        k = key_of(it["title"])
        if k in seen or k in batch:
            continue
        batch.add(k)
        seen[k] = {"t": it["title"][:80], "d": today}
        age = now - it["date"] if it["date"] else timedelta(0)
        if kind == "event" and age <= max_age:
            new.append(it)
        elif kind == "goods" and age <= goods_max_age and not goods_first:
            goods.append(it)
    if goods_enabled and texts:
        seen["__goods_seeded__"] = {"t": "meta", "d": today}

    # 記録は半年分だけ残す（目印の __ で始まるものは残す）
    cutoff = (now - timedelta(days=180)).astimezone(JST).date().isoformat()
    seen = {k: v for k, v in seen.items() if k.startswith("__") or v.get("d", "") >= cutoff}
    if texts:
        SEEN.parent.mkdir(exist_ok=True)
        SEEN.write_text(json.dumps(seen, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")

    def line(it: dict) -> str:
        d = it["date"].astimezone(JST) if it["date"] else None
        meta = "・".join(x for x in (it["source"], f"{d.month}/{d.day}" if d else "") if x)
        return f"🔸 {it['title']}\n　{meta}\n　{it['link']}"

    for old_file in ROOT.glob(MESSAGE_GLOB):
        old_file.unlink()

    blocks = []  # 吹き出しに詰めていく文章のかたまり
    if first_run and texts:
        blocks.append("📣 マイメロディの外部イベント情報の見張りを開始しました。\n"
                      "見つかったらお知らせします（AI判定なしのため、関係ない記事が混ざることがあります）。")
        if new:
            blocks.append(f"▼ 直近の候補（{len(new)}件）")
    elif new:
        blocks.append(f"📣 マイメロディの外部イベント候補（{len(new)}件）")
    blocks += [line(i) for i in new]
    if goods_first and texts and not first_run:
        blocks.append("🛍️ マイメロディのグッズ情報の通知を開始しました。次の新着からお知らせします。")
    if goods:
        blocks.append(f"🛍️ マイメロディのグッズ情報（{len(goods)}件）")
        blocks += [line(i) for i in goods]

    if blocks:
        # 1回の送信で吹き出しは最大5つ。ピューロランドの通知用に1つ空けておく
        max_bubbles = int(os.environ.get("MYMELO_MAX_BUBBLES", "4"))
        bubbles, cur, dropped = [], "", 0
        for i, b in enumerate(blocks):
            add = ("\n\n" if cur else "") + b
            if cur and len(cur) + len(add) > BUBBLE_CHARS:
                if len(bubbles) == max_bubbles - 1:
                    dropped = sum(1 for x in blocks[i:] if x.startswith("🔸"))
                    break
                bubbles.append(cur)
                cur, add = b, b
                continue
            cur += add
        if dropped:
            cur += f"\n\nほか {dropped}件（入りきりませんでした）"
        bubbles.append(cur)
        for n, text in enumerate(bubbles, 1):
            (ROOT / f"message_mymelo_{n}.txt").write_text(text.strip(), encoding="utf-8")
        print("\n\n----- 吹き出しの区切り -----\n\n".join(bubbles))
        print(f"\n（吹き出し {len(bubbles)}個 / イベント {len(new)}件 / グッズ {len(goods)}件）")
    else:
        print(f"新しい候補なし（記事{len(items)}件を確認）")

    if errors and not texts:
        with open(ROOT / "errors.md", "a", encoding="utf-8") as f:
            f.write("\n".join(errors) + "\n")
    elif errors:
        print("\n".join(errors))  # 一部の取得失敗は Issue にしない（次回また取りに行く）


if __name__ == "__main__":
    main()
