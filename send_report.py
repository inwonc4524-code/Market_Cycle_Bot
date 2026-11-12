"""
점검·검증 보고를 휴대폰 알림(ntfy)으로 보내요.
reports/ 폴더에 보고 파일(JSON)이 올라오면 GitHub Actions(report.yml)가 이 파일을 실행해요.

보고 파일 모양:
{
  "title": "✅ 토요일 점검 검증 통과",            # 알림 제목
  "kicker": "검증 결과",                          # 신문 카드 빨간 이름표
  "headline": "오늘 아침 점검 4개 모두 이상 없음",  # 신문 카드 큰 제목
  "items": [                                     # 카드·본문 목록 (한 줄씩)
    {"head": "사이클 봇 · 통과", "body": "이야기 7개 나라 숫자 뉴스와 일치", "meta": "화면 시험 96/96"},
    "ETF 봇 · 오늘 변경 없음"
  ],
  "ok": true                                     # 문제가 있었으면 false (알림을 더 눈에 띄게)
}
"""
import json
import os
import sys

import newscard

DASH = "https://inwonc4524-code.github.io/Market_Cycle_Bot/"


def main(path):
    with open(path, encoding="utf-8") as f:
        r = json.load(f)
    items = r.get("items", [])
    lines = []
    for it in items:
        if isinstance(it, dict):
            s = it.get("head", "")
            if it.get("body"):
                s += "\n   " + it["body"]
            if it.get("meta"):
                s += "\n   (" + it["meta"] + ")"
            lines.append(("• " if r.get("ok", True) else "• ") + s)
        else:
            lines.append("• " + str(it))
    msg = "\n\n".join(lines)
    ok = r.get("ok", True)
    card = {"paper": r.get("paper", "점검 신문"), "kicker": r.get("kicker", "검증 결과"),
            "headline": r.get("headline", r.get("title", "")), "lines": items,
            "footer": r.get("footer", "자세한 내용은 Claude 대화에서")}
    newscard.send(os.environ.get("NTFY_TOPIC"), r.get("title", "점검 보고"), newscard.tidy(msg, link_hint=False),
                  click=r.get("click", DASH), tags=["white_check_mark" if ok else "warning"],
                  priority=3 if ok else 4, card=card)


if __name__ == "__main__":
    main(sys.argv[1])
