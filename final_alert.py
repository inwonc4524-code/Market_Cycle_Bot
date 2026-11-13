"""하루 마무리 알림 — 아시아 여섯 나라의 저녁 화면 업데이트와 1일 이야기가 모두 끝나면 한 번만 보내요.

조건 (평일, 한국 시간 저녁 6시 이후):
  1) 오늘 저녁 알림(evening)이 이미 나갔고
  2) 오늘 장이 열린 아시아 나라는 모두 1일 이야기(stories.json)가 오늘 날짜로 써졌을 때
보낸 뒤 docs/alerts_sent.txt 에 "날짜 final" 을 적어서 두 번 보내지 않아요.
봇 실행 끝과, Claude 이야기 작업이 끝날 때 둘 다 이 파일을 불러요 (먼저 조건이 맞는 쪽이 보냄).
"""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import newscard
from analyze import DASH_URL, FLAG

ASIA = ["kr", "jp", "tw", "cn", "hk", "sg"]
HERE = os.path.dirname(os.path.abspath(__file__))
SENT = os.path.join(HERE, "docs", "alerts_sent.txt")


def main():
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    today = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5 or now.hour < 18:
        print("평일 저녁 6시 이후에만 보내요")
        return
    sent = open(SENT, encoding="utf-8").read().split("\n") if os.path.exists(SENT) else []
    if f"{today} final" in sent:
        print("오늘 마무리 알림은 이미 보냈어요")
        return
    if f"{today} evening" not in sent:
        print("아직 저녁 알림 전이라 기다려요")
        return
    data = json.load(open(os.path.join(HERE, "docs", "data.json"), encoding="utf-8"))
    st = json.load(open(os.path.join(HERE, "docs", "stories.json"), encoding="utf-8"))
    lines, wait = [], []
    for m in ASIA:
        last = (data.get("markets", {}).get(m) or {}).get("last_date")
        if last != today:            # 오늘 쉬는 날이면 건너뜀
            continue
        if st.get("story_dates", {}).get(f"{m}:d1") != today:
            wait.append(FLAG[m])
            continue
        d1 = (st.get("R", {}).get(m) or {}).get("d1") or {}
        hero = (d1.get("hero") or [""])[0]
        lines.append(f"{FLAG[m]} {d1.get('chg', '')} · 주인공: {hero}".strip())
    if wait:
        print("아직 이야기가 안 써진 나라:", ", ".join(wait))
        return
    if not lines:
        print("오늘 장이 열린 아시아 나라가 없어요")
        return
    msg = "오늘 아시아 나라 화면과 1일 이야기가 모두 업데이트됐어요 📰\n\n" + "\n".join(lines)
    print(msg)
    if "--dry" not in sys.argv:
        newscard.send(os.environ.get("NTFY_TOPIC"), "🏁 오늘 아시아 정리 끝 · 이어달리기", newscard.tidy(msg),
                      click=DASH_URL, tags=["checkered_flag"], priority=3,
                      actions=[{"action": "view", "label": "대시보드 열기", "url": DASH_URL}])
    with open(SENT, "a", encoding="utf-8") as f:
        f.write(f"{today} final\n")


if __name__ == "__main__":
    main()
