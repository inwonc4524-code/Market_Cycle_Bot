import json, os, sys, requests
tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
name = os.environ.get("BOT_NAME", "봇")
def note(level, msg):
    print(f"::{level} title=텔레그램 시험::{msg}")
if not tok: note("error", "TELEGRAM_BOT_TOKEN 비밀값이 비어 있어요"); sys.exit(1)
if not chats: note("error", "TELEGRAM_CHAT_ID 비밀값이 비어 있어요"); sys.exit(1)
me = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=20).json()
if not me.get("ok"):
    note("error", f"토큰이 틀렸어요 (getMe: {me.get('description')})"); sys.exit(1)
bot = "@" + me["result"]["username"]
note("notice", f"이 저장소의 토큰은 {bot} 봇이에요. 받는 사람 수: {len(chats)}, ID 모양: " + ", ".join(("숫자" if c.lstrip('-').isdigit() else "숫자 아님!") + f"({len(c)}자리)" for c in chats))
bad = False
for c in chats:
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": c, "text": f"🧪 {name} 텔레그램 시험\n\n이 메시지가 보이면 연결 성공이에요!"}, timeout=20).json()
    if r.get("ok"):
        note("notice", f"{bot} → ID {len(c)}자리: 보내기 성공")
    else:
        bad = True
        note("error", f"{bot} → ID {len(c)}자리: 실패 — {r.get('description')}")
sys.exit(1 if bad else 0)
