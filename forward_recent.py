"""
ntfy 에 최근(약 12시간) 보낸 알림을 텔레그램으로 다시 보내요.
같은 ntfy 채널을 여러 봇이 같이 쓸 수 있어서, 알림에 붙은 링크로 '이 봇의 알림'만 골라요.
"""
import datetime as dt
import html
import json
import os
import sys
import time

import requests

TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
TOK = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHATS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
REPO = os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1].lower()
MODE = os.environ.get("MATCH_MODE", "repo")      # repo: 링크에 이 저장소 이름 / not_pages: github.io 링크가 아닌 것
LIMIT = int(os.environ.get("LIMIT", "10") or 10)
KST = dt.timezone(dt.timedelta(hours=9))


def note(level, msg):
    print(f"::{level} title=최근 알림 다시 보내기::{msg}")


if not (TOPIC and TOK and CHATS):
    note("error", "NTFY_TOPIC / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 중 비어 있는 게 있어요")
    sys.exit(1)

r = requests.get(f"https://ntfy.sh/{TOPIC}/json", params={"poll": "1", "since": "24h"}, timeout=60)
r.raise_for_status()
msgs = []
for line in r.text.splitlines():
    try:
        m = json.loads(line)
    except ValueError:
        continue
    if m.get("event") != "message":
        continue
    links = [m.get("click") or ""] + [a.get("url", "") for a in m.get("actions") or []]
    links = " ".join(links).lower()
    if MODE == "repo":
        if f"/{REPO}/" not in links and not links.rstrip("/ ").endswith(f"/{REPO}"):
            continue
    else:
        if "github.io" in links:
            continue
    if "텔레그램 시험" in (m.get("title") or ""):
        continue
    msgs.append(m)

msgs = msgs[-LIMIT:]
note("notice", f"ntfy 에서 이 봇의 최근 알림 {len(msgs)}개를 찾았어요 (ntfy 는 약 12시간치만 보관해요)")
if not msgs:
    for c in CHATS:
        requests.post(f"https://api.telegram.org/bot{TOK}/sendMessage", timeout=20, json={
            "chat_id": c, "text": "📭 ntfy 에 남아 있는 최근 알림이 없어요 (ntfy 는 약 12시간치만 보관해요).\n다음 정해진 시간부터 여기로 바로 와요."})
    sys.exit(0)

sent = 0
for m in msgs:
    when = dt.datetime.fromtimestamp(m["time"], KST).strftime("%m/%d %H:%M")
    title = m.get("title") or "알림"
    body = (m.get("message") or "").replace("​", "").strip()
    if body.endswith("👉 눌러서 자세히 보기"):
        body = body[: -len("👉 눌러서 자세히 보기")].strip()
    text = f"<b>{html.escape(title)}</b>\n<i>⏪ {when} 에 보냈던 알림</i>\n\n{html.escape(body)}"[:4000]
    rows = []
    for a in (m.get("actions") or [])[:3]:
        if a.get("action") == "view" and str(a.get("url", "")).startswith("http"):
            rows.append([{"text": "👉 " + a.get("label", "열기"), "url": a["url"]}])
    if m.get("click", "").startswith("http") and all(m["click"] != r_[0]["url"] for r_ in rows):
        rows.append([{"text": "👉 눌러서 자세히 보기", "url": m["click"]}])
    for c in CHATS:
        body_ = {"chat_id": c, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
                 "disable_notification": True}
        if rows:
            body_["reply_markup"] = {"inline_keyboard": rows}
        res = requests.post(f"https://api.telegram.org/bot{TOK}/sendMessage", json=body_, timeout=20)
        if res.status_code == 429:
            time.sleep(int(res.json().get("parameters", {}).get("retry_after", 3)) + 1)
            res = requests.post(f"https://api.telegram.org/bot{TOK}/sendMessage", json=body_, timeout=20)
        if res.ok:
            sent += 1
        else:
            note("warning", f"'{title[:30]}' 보내기 실패: {res.json().get('description')}")
        time.sleep(1.1)
note("notice", f"텔레그램으로 {sent}개 보냈어요")
