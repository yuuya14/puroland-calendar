#!/usr/bin/env python3
"""サンリオピューロランドのグリーティング日程から、指定キャラクターの出演を検出する。

  - 公式サイトのスケジュールページが読み込んでいる日別データ（JSON）を、今日から数週間先まで取得
  - config.json の characters に一致する出演枠を data/appearances.json に記録
  - docs/greeting.ics（iPhoneで購読するカレンダー）を作り直す
  - 前回から変わった出演予定（新規・変更・取りやめ）を message.txt に書き出す（LINE通知用）
  - 取得・解析に失敗したら errors.md に書き出す（GitHub Issue 用）

使い方: python3 piano_watch.py [--offline-dir DIR]   # DIR に YYYYMMDD.json を置くとそれを使う（テスト用）
"""
import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "data" / "appearances.json"
PENDING = ROOT / "data" / "pending.json"   # 変更・取りやめの確認待ち（2回続けて同じ結果なら確定）
ICS = ROOT / "docs" / "greeting.ics"
JST = ZoneInfo("Asia/Tokyo")
WEEKDAYS = "月火水木金土日"
DAY_URL = "https://www.puroland.jp/system/json/schedule/day/{ymd}.json?{ts}"
SCHEDULE_URL = "https://www.puroland.jp/schedule/?id=greeting"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130 Safari/537.36")
KEEP_PAST_DAYS = 90


# ---------------------------------------------------------------- 取得
def fetch_day(d: date, offline: Path | None):
    """その日のJSONを返す。未公開（404）なら None。"""
    ymd = d.strftime("%Y%m%d")
    if offline:
        p = offline / f"{ymd}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    url = DAY_URL.format(ymd=ymd, ts=int(time.time() * 1000))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": SCHEDULE_URL})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
            if attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(3)
    return None


# ---------------------------------------------------------------- 解析
def to_min(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def count_entries(data: dict) -> int:
    """その日のグリーティング枠（全キャラクター）の数。0なら未公開か更新途中とみなす。"""
    greeting = (data.get("schedule") or {}).get("greeting") or {}
    n = 0
    for section in greeting.values():
        if isinstance(section, dict):
            e = section.get("schedule")
            n += len(e) if isinstance(e, (dict, list)) else 0
    return n


def parse_day(data: dict, slugs: set[str]) -> list[dict]:
    """1日分のJSONから、対象キャラクターの出演枠を取り出す。"""
    greeting = (data.get("schedule") or {}).get("greeting") or {}
    found = []
    for section in greeting.values():
        if not isinstance(section, dict):
            continue
        entries = section.get("schedule")
        if isinstance(entries, dict):
            entries = list(entries.values())
        if not isinstance(entries, list) or not entries:
            continue
        place = ((section.get("post") or {}).get("title") or "").strip()
        floor = ((section.get("floor") or {}).get("title") or "").strip()
        sec_time = (section.get("time") or {}).get("start_end") or [{}]
        sec_end = (sec_time[0] or {}).get("end", "") if isinstance(sec_time, list) else ""

        # 部屋ごとに開始時刻順に並べ、次の枠の開始＝この枠の終了とする
        rows = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            se = ((e.get("time") or {}).get("start_end") or [{}])
            se = se[0] if isinstance(se, list) and se else {}
            start = se.get("start", "")
            if not start:
                continue
            rows.append({"room": (e.get("room") or "").strip(), "start": start,
                         "end": se.get("end", ""),
                         "chars": [c for c in (e.get("character") or []) if isinstance(c, dict)]})
        rows.sort(key=lambda r: (r["room"], to_min(r["start"])))
        for i, r in enumerate(rows):
            hit = [c for c in r["chars"] if c.get("slug") in slugs]
            if not hit:
                continue
            end = r["end"]
            if not end:
                nxt = next((x for x in rows[i + 1:] if x["room"] == r["room"]), None)
                end = nxt["start"] if nxt else sec_end
            for c in hit:
                found.append({"slug": c["slug"], "name": c.get("name", ""),
                              "place": place, "floor": floor, "room": r["room"],
                              "start": r["start"], "end": end or ""})
    found.sort(key=lambda s: (to_min(s["start"]), s["room"], s["slug"]))
    return found


# ---------------------------------------------------------------- 表示用
def md(ds: str) -> str:
    d = date.fromisoformat(ds)
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def slot_text(s: dict) -> str:
    t = f"{s['start']}〜{s['end']}" if s["end"] else f"{s['start']}〜"
    where = " ".join(x for x in (s["place"], s["room"]) if x)
    return f"{t}　{where}"


def key(s: dict) -> tuple:
    return (s["slug"], s["place"], s["room"], s["start"], s["end"])


# ---------------------------------------------------------------- カレンダー
def esc(t: str) -> str:
    return t.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > (75 if not out else 74):
            out.append(cur)
            cur, size = ch, n
        else:
            cur, size = cur + ch, size + n
    out.append(cur)
    return "\r\n ".join(out)


def build_ics(state: dict, cfg: dict) -> str:
    emoji = {c["slug"]: c.get("emoji", "") for c in cfg["characters"]}
    rh, rm = map(int, cfg.get("reminder_time", "20:00").split(":"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fmt = "%Y%m%dT%H%M%SZ"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//puroland-calendar//JA",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{esc(cfg.get('calendar_name', 'ピューロランド グリーティング'))}",
             "X-WR-TIMEZONE:Asia/Tokyo", "REFRESH-INTERVAL;VALUE=DURATION:PT2H",
             "X-PUBLISHED-TTL:PT2H"]
    for ds in sorted(state):
        d = date.fromisoformat(ds)
        first_of_day = {}
        for s in state[ds]:
            sh, sm = map(int, s["start"].split(":"))
            start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=JST)
            if s["end"]:
                eh, em = map(int, s["end"].split(":"))
                end = datetime(d.year, d.month, d.day, eh, em, tzinfo=JST)
            else:
                end = start + timedelta(hours=1)
            if end <= start:
                end = start + timedelta(hours=1)
            title = (f"{cfg.get('title_prefix', '')}{emoji.get(s['slug'], '')} "
                     f"{s['name']}（{s['room'] or s['place']}）").strip()
            desc = "\n".join(x for x in (
                f"{s['place']}（{s['floor']}）" if s["floor"] else s["place"],
                s["room"], f"{s['start']}〜{s['end']}", "", SCHEDULE_URL) if x is not None)
            uid = f"{s['slug']}-{ds}-{s['room'] or s['place']}-{s['start']}@puroland-calendar"
            lines += ["BEGIN:VEVENT", f"UID:{esc(uid)}", f"DTSTAMP:{stamp}",
                      f"DTSTART:{start.astimezone(timezone.utc).strftime(fmt)}",
                      f"DTEND:{end.astimezone(timezone.utc).strftime(fmt)}",
                      f"SUMMARY:{esc(title)}",
                      f"LOCATION:{esc('サンリオピューロランド ' + s['place'])}",
                      f"DESCRIPTION:{esc(desc)}", f"URL:{SCHEDULE_URL}"]
            # 前日の通知は、その日・そのキャラクターの最初の枠にだけ付ける
            if s["slug"] not in first_of_day:
                first_of_day[s["slug"]] = True
                remind = datetime(d.year, d.month, d.day, rh, rm, tzinfo=JST) - timedelta(days=1)
                lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                          f"DESCRIPTION:{esc('明日は ' + s['name'] + ' のグリーティングがあります')}",
                          f"TRIGGER;VALUE=DATE-TIME:{remind.astimezone(timezone.utc).strftime(fmt)}",
                          "END:VALARM"]
            lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline-dir")
    ap.add_argument("--today")  # テスト用 YYYY-MM-DD
    args = ap.parse_args()
    offline = Path(args.offline_dir) if args.offline_dir else None

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    slugs = {c["slug"] for c in cfg["characters"]}
    first_run = not STATE.exists()
    old: dict = {} if first_run else json.loads(STATE.read_text(encoding="utf-8"))
    pending: dict = json.loads(PENDING.read_text(encoding="utf-8")) if PENDING.exists() else {}
    pending_before = json.dumps(pending, sort_keys=True)
    new = {k: v for k, v in old.items()}
    today = date.fromisoformat(args.today) if args.today else datetime.now(JST).date()

    errors, fetched, misses, responded = [], [], 0, 0
    for i in range(cfg.get("days_ahead", 45)):
        d = today + timedelta(days=i)
        try:
            data = fetch_day(d, offline)
        except Exception as e:  # noqa: BLE001
            errors.append(f"- {md(d.isoformat())} のデータを取得できませんでした: {e}")
            continue
        if data is None:
            misses += 1
            if misses >= 3 and i >= 7:  # 未公開の日が続いたら打ち切り
                break
            continue
        misses = 0
        responded += 1
        ds = d.isoformat()
        if count_entries(data) == 0:
            continue  # グリーティングの枠が1つもない日（休館日・未公開・更新途中）は判定しない
        try:
            slots = parse_day(data, slugs)
        except Exception as e:  # noqa: BLE001
            errors.append(f"- {md(ds)} のデータを解析できませんでした: {e}")
            continue
        fetched.append(ds)
        prev = old.get(ds, [])
        if sorted(map(key, prev)) == sorted(map(key, slots)):
            pending.pop(ds, None)
            continue
        if not prev:
            new[ds] = slots  # 新しく掲載された → すぐ反映
            pending.pop(ds, None)
            continue
        if not slots and d == today:
            continue  # 当日は終わった枠がデータから消えることがあるため、記録を残す
        # 既にある予定の変更・取りやめは、サイトの更新途中を拾わないよう2回続けて確認できたら反映
        if pending.get(ds) == slots:
            pending.pop(ds, None)
            if slots:
                new[ds] = slots
            else:
                new.pop(ds, None)
        else:
            pending[ds] = slots

    if not fetched:
        if responded and not errors:
            print("グリーティングの枠が載っている日がありません（更新途中の可能性）。今回は判定しません。")
            return
        if not errors:
            errors.append("- 公開されている日別データが1日分も見つかりませんでした。サイトの構成が変わった可能性があります。")
        (ROOT / "errors.md").write_text("\n".join(errors) + "\n", encoding="utf-8")
        print("\n".join(errors))
        return  # 何も取れなかったときは記録も通知も変更しない

    # 古い記録は削除
    cutoff = (today - timedelta(days=KEEP_PAST_DAYS)).isoformat()
    new = {k: v for k, v in sorted(new.items()) if k >= cutoff}

    # 差分（今日以降のみ）
    emo = {c["slug"]: c.get("emoji", "") for c in cfg["characters"]}
    lines = []
    for ds in sorted(set(old) | set(new)):
        if ds < today.isoformat():
            continue
        o, n = old.get(ds, []), new.get(ds, [])
        if sorted(map(key, o)) == sorted(map(key, n)):
            continue
        if not o:
            lines += [f"🆕 {md(ds)}"] + [f"　{emo.get(s['slug'], '')}{s['name']}　{slot_text(s)}" for s in n]
        elif not n:
            lines += [f"🗑️ {md(ds)} の掲載がなくなりました"]
        else:
            lines += [f"✏️ {md(ds)} の内容が変わりました"] + [f"　{emo.get(s['slug'], '')}{s['name']}　{slot_text(s)}" for s in n]

    STATE.parent.mkdir(exist_ok=True)
    pending = {k: v for k, v in pending.items() if k >= today.isoformat()}
    if json.dumps(pending, sort_keys=True) != pending_before:
        PENDING.write_text(json.dumps(pending, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if new != old or first_run:
        STATE.write_text(json.dumps(new, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        ICS.parent.mkdir(exist_ok=True)
        ICS.write_text(build_ics(new, cfg), encoding="utf-8", newline="")

    names = "・".join(f"{c.get('emoji', '')}{c['name']}" for c in cfg["characters"])
    if lines:
        head = (f"{names}の通知を開始しました。\n現在掲載されている出演予定です。"
                if first_run else f"{names}のグリーティング情報が更新されました。")
        msg = "\n".join([head, "", *lines, "", f"📅 {SCHEDULE_URL}"])
        (ROOT / "message.txt").write_text(msg, encoding="utf-8")
        print(msg)
    elif first_run:
        msg = (f"{names}の通知を開始しました。\n現在、掲載されている出演予定はありません。"
               "掲載されたらお知らせします。")
        (ROOT / "message.txt").write_text(msg, encoding="utf-8")
        print(msg)
    else:
        print(f"変更なし（確認した日数: {len(fetched)}）")
    if errors:
        (ROOT / "errors.md").write_text("\n".join(errors) + "\n", encoding="utf-8")
        print("\n".join(errors))


if __name__ == "__main__":
    main()
