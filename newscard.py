"""
신문 카드 알림 (ntfy)
─────────────────────
휴대폰 알림을 '신문 기사'처럼 보이게 해요.
  1) 제목·본문을 정리해서 보내고
  2) 조선일보 명조체로 그린 작은 신문 카드 그림(PNG)을 알림에 붙여요.
     (갤럭시에서 알림을 펼치면 그림이 크게 보여요)

그림을 못 만들면(글씨체를 못 받았거나 오류) 예전처럼 글자만 보내요.
네 봇(이어달리기·ETF·개별종목·주식뉴스)이 같은 파일을 써요.
"""
import io
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
FONT_URL = "https://cdn.jsdelivr.net/gh/projectnoonnu/noonfonts_one@1.0/Chosunilbo_myungjo.woff"
FONT_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fontcache", "ChosunIlboMyungjo.ttf")

W, H = 1200, 630                      # 갤럭시 알림 그림에 잘 맞는 가로 2:1 정도
PAPER, INK, GRAY, RULE = "#FBF8F1", "#1A1A1A", "#6B6B6B", "#2B2B2B"
UP, DOWN, KICK = "#C62828", "#1E5AA8", "#B71C1C"   # 한국식: 오르면 빨강, 내리면 파랑

_font_path = None
_cmap = None


def _get_font():
    """조선일보 명조체를 받아서 TTF 로 바꿔 둬요 (한 번만)."""
    global _font_path, _cmap
    if _font_path:
        return _font_path
    path = os.environ.get("NEWSCARD_FONT")          # 시험용으로 다른 글씨체를 쓸 때
    if not path:
        path = FONT_CACHE
        if not os.path.exists(path):
            from fontTools.ttLib import TTFont
            r = requests.get(FONT_URL, timeout=60)
            r.raise_for_status()
            f = TTFont(io.BytesIO(r.content))
            f.flavor = None
            os.makedirs(os.path.dirname(path), exist_ok=True)
            f.save(path)
    try:
        from fontTools.ttLib import TTFont
        f = TTFont(path, fontNumber=0)
        _cmap = set(f.getBestCmap().keys())
    except Exception:
        _cmap = None
    _font_path = path
    return path


def clean(text):
    """글씨체에 없는 글자(이모지 등)를 빼고 공백을 정리해요."""
    out = []
    for ch in str(text):
        cp = ord(ch)
        if ch in "\n\t ":
            out.append(" ")
        elif _cmap is not None and cp not in _cmap:
            continue
        elif _cmap is None and (cp >= 0x1F000 or 0x2600 <= cp <= 0x27BF or 0xFE00 <= cp <= 0xFE0F):
            continue
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip(" ·:-")


def _wrap(draw, text, font, width, max_lines):
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) <= width:
            cur += ch
        else:
            # 단어 중간에서 끊기지 않게 마지막 빈칸에서 자르기
            cut = cur.rfind(" ")
            if cut > len(cur) * 0.5:
                lines.append(cur[:cut])
                cur = cur[cut + 1:] + ch
            else:
                lines.append(cur)
                cur = ch
            if len(lines) == max_lines:
                break
    if len(lines) < max_lines and cur:
        lines.append(cur)
    joined = "".join(lines)
    if len(joined.replace(" ", "")) < len(text.replace(" ", "")):     # 넘친 글은 … 로
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


_PCT = re.compile(r"([+\-−]\d[\d,]*(?:\.\d+)?%p?|▲\s?[\d.,]+%?|▼\s?[\d.,]+%?)")


def _draw_colored(draw, xy, text, font, width):
    """+3.2% 는 빨강, -1.5% 는 파랑으로 칠하면서 한 줄 쓰기 (넘치면 …)."""
    x, y = xy
    while text and draw.textlength(text, font=font) > width:
        text = text[:-2] + "…" if len(text) > 2 else ""
    for part in _PCT.split(text):
        if not part:
            continue
        color = INK
        if _PCT.fullmatch(part):
            color = UP if part[0] in "+▲" else DOWN
        draw.text((x, y), part, font=font, fill=color, stroke_width=1 if color != INK else 0, stroke_fill=color)
        x += draw.textlength(part, font=font)


def render(paper, kicker, headline, lines, footer="눌러서 대시보드에서 자세히 보기", date=None):
    """신문 카드 PNG(bytes)를 만들어요."""
    from PIL import Image, ImageDraw, ImageFont
    fp = _get_font()
    F = lambda s: ImageFont.truetype(fp, s)
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    M = 56
    date = date or datetime.now(KST)
    wk = "월화수목금토일"[date.weekday()]

    # ── 신문 머리 (제호)
    f_mast = F(58)
    name = clean(paper)
    tw = d.textlength(name, font=f_mast)
    d.text(((W - tw) / 2, 26), name, font=f_mast, fill=INK, stroke_width=1, stroke_fill=INK)
    f_small = F(24)
    d.text((M, 50), f"{date.year}년 {date.month}월 {date.day}일 ({wk})", font=f_small, fill=GRAY)
    right = f"{date:%H:%M} 발행"
    d.text((W - M - d.textlength(right, font=f_small), 50), right, font=f_small, fill=GRAY)
    d.line((M, 104, W - M, 104), fill=RULE, width=4)
    d.line((M, 112, W - M, 112), fill=RULE, width=1)

    # ── 꼭지 (빨간 작은 이름표)
    y = 134
    f_k = F(26)
    k = clean(kicker)
    if k:
        kw = d.textlength(k, font=f_k)
        d.rectangle((M, y, M + kw + 24, y + 40), fill=KICK)
        d.text((M + 12, y + 5), k, font=f_k, fill="white", stroke_width=1, stroke_fill="white")
        y += 56

    # ── 큰 제목 (최대 2줄)
    f_h = F(50)
    for ln in _wrap(d, clean(headline), f_h, W - 2 * M, 2):
        _draw_colored(d, (M, y), ln, f_h, W - 2 * M)
        y += 64
    y += 6
    d.line((M, y, W - M, y), fill="#BDB6A6", width=1)
    y += 16

    # ── 본문 (핵심 줄 몇 개)
    f_b = F(31)
    room = int((H - 70 - y) // 44)
    shown = 0
    items = [clean(x) for x in lines]
    items = [x for x in items if x]
    per = 3 if len(items) <= 2 else 1          # 줄이 적으면(속보 요약 등) 한 항목을 여러 줄로
    for t in items:
        if shown >= room:
            break
        d.ellipse((M, y + 14, M + 8, y + 22), fill=INK)
        for part in _wrap(d, t, f_b, W - 2 * M - 22, min(per, room - shown)):
            _draw_colored(d, (M + 22, y), part, f_b, W - 2 * M - 22)
            y += 44
            shown += 1

    # ── 아랫단
    d.line((M, H - 58, W - M, H - 58), fill=RULE, width=1)
    f_f = F(24)
    ft = clean(footer)
    d.text((W - M - d.textlength(ft + " →", font=f_f), H - 46), ft + " →", font=f_f, fill=GRAY)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _upload(topic, png):
    """그림을 ntfy 에 올려 주소를 받아요 (구독자 없는 따로 칸에 올려서 알림이 두 번 가지 않게)."""
    r = requests.put(f"https://ntfy.sh/{topic}_card", data=png,
                     headers={"Filename": "news.png", "Cache": "no"}, timeout=30)
    r.raise_for_status()
    return r.json()["attachment"]["url"]


def send(topic, title, message, click=None, tags=None, priority=3, actions=None, card=None):
    """
    알림 보내기.
    card = {"paper": "이어달리기 신문", "kicker": "마감 시황", "headline": "...", "lines": [...]}
    """
    if not topic:
        print("[NTFY_TOPIC 없음 — 알림 대신 화면에 출력]\n" + title + "\n" + message + "\n")
        return True
    body = {"topic": topic, "title": title, "message": message[:3900], "priority": priority}
    if click:
        body["click"] = click
    if actions:
        body["actions"] = actions[:3]
    if tags:
        body["tags"] = tags
    if card:
        try:
            png = render(card.get("paper", ""), card.get("kicker", ""), card.get("headline", title),
                         card.get("lines", []), card.get("footer", "눌러서 대시보드에서 자세히 보기"))
            body["attach"] = _upload(topic, png)
            body["filename"] = "news.png"
        except Exception as e:
            print("  ! 신문 카드 그림 실패 — 글자만 보내요:", e)
    for attempt in range(3):
        try:
            r = requests.post("https://ntfy.sh/", data=json.dumps(body).encode("utf-8"),
                              headers={"Content-Type": "application/json"}, timeout=20)
            print("ntfy 전송:", r.status_code)
            r.raise_for_status()
            return True
        except Exception as e:
            print(f"  ! 알림 전송 실패 ({attempt + 1}/3): {e}")
            time.sleep(3)
    return False


def tidy(message, link_hint=True):
    """본문 정리: 빈 줄 여러 개를 하나로, 맨 끝에 '눌러서 보기' 안내."""
    msg = re.sub(r"\n{3,}", "\n\n", message.strip())
    if link_hint:
        msg += "\n\n👉 눌러서 자세히 보기"
    return msg


if __name__ == "__main__":      # 시험: python newscard.py → card_test.png
    png = render("이어달리기 신문", "마감 시황", "코스피 +4.39% 급반등… 건설·조선·2차전지 앞장",
                 ["🇰🇷 코스피 +4.39% · 오늘 1등 🏗️건설 +8.0%", "🇯🇵 닛케이225 +2.87% · 오늘 1등 반도체 +4.4%",
                  "🇨🇳 상하이종합 +0.08% · 오늘 1등 전자·통신장비 +3.7%", "🇺🇸 S&P500 -0.42% · 오늘 1등 에너지 +1.2%",
                  "🇹🇼 가권지수 +2.52%", "🇸🇬 STI +1.61%"])
    open("card_test.png", "wb").write(png)
    print(len(png), "bytes")
