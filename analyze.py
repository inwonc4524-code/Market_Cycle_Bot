"""
🏃 주식시장 이어달리기 봇 — 업종 계산기

업종별 가격(업종 ETF)을 받아서
  · 기간별(1일~20년) 업종 성적표와 '주인공 업종'
  · 6개월 성적 1등이 바뀐 날 = '바통이 넘어간 날'
  · 대표 지수가 최고점보다 20% 넘게 떨어진 때 = '겨울'
을 계산해서 docs/data.json 에 저장하고, 휴대폰(ntfy)으로 요약을 보냅니다.

  python analyze.py          # 실제 데이터 (야후 파이낸스)
  python analyze.py --demo   # 가짜 데이터로 동작 확인 (인터넷 없이)

※ 과거 가격으로 계산한 공부용 자료예요. 투자 권유가 아닙니다.
"""
import argparse
import csv
import json
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
DASH_URL = "https://inwonc4524-code.github.io/Market_Cycle_Bot/"

# 1단계: 한국·미국·일본  2단계: 대만·홍콩·중국  3단계: 싱가포르
BENCH = {
    "kr": ("^KS11", "코스피"),
    "us": ("^GSPC", "S&P500"),
    "jp": ("^N225", "닛케이225"),
    "tw": ("^TWII", "가권지수"),
    "hk": ("^HSI", "항셍지수"),
    "cn": ("000001.SS", "상하이종합지수"),
    "sg": ("^STI", "STI"),
}
FLAG = {"kr": "🇰🇷 한국", "us": "🇺🇸 미국", "jp": "🇯🇵 일본",
        "tw": "🇹🇼 대만", "hk": "🇭🇰 홍콩", "cn": "🇨🇳 중국", "sg": "🇸🇬 싱가포르"}
GOLD = "GC=F"

WINDOWS = [  # (키, 이름, 거래일 수)
    ("d1", "1일", 1), ("w1", "1주일", 5), ("m1", "1달", 21), ("m6", "6개월", 126),
    ("y1", "1년", 252), ("y3", "3년", 756), ("y5", "5년", 1260),
    ("y10", "10년", 2520), ("y20", "20년", 5040),
]
LEAD_DAYS = 126     # 바통 판단: 최근 6개월 성적 1등
MIN_HOLD = 2        # 2달 이상 1등을 지켜야 '바통을 잡았다'고 봐요 (잠깐 1등은 무시)
MIN_SECTORS = 3     # 업종이 3개 이상 있을 때부터 계산
WINTER = -0.20      # 최고점보다 20% 넘게 하락 = 겨울
CORRECTION = -0.10  # 10% 넘게 하락 = 조정


# ───────────────────────── 1. 데이터 받기 ─────────────────────────

def load_sectors():
    with open(os.path.join(ROOT, "sectors.csv"), encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r["market"] in BENCH]


def _close(raw, t, many):
    s = raw[t]["Close"] if many else raw["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    s = s.dropna()
    return s[s > 0]


def fetch_prices(tickers):
    """야후 파이낸스에서 상장 이후 전체 기간의 가격(배당 반영)을 받아옵니다."""
    import yfinance as yf

    out = {}
    raw = yf.download(tickers, period="max", auto_adjust=True, progress=False,
                      group_by="ticker", threads=True)
    for t in tickers:
        try:
            s = _close(raw, t, len(tickers) > 1)
            if len(s):
                out[t] = s
        except Exception:
            pass
    for t in [t for t in tickers if t not in out]:   # 못 받은 건 하나씩 한 번 더
        try:
            r = yf.download(t, period="max", auto_adjust=True, progress=False)
            s = _close(r, t, False)
            if len(s):
                out[t] = s
        except Exception:
            pass
    return out


def basket(prices, ticker):
    """'A|B|C' 처럼 여러 회사를 묶은 업종은 똑같은 비중으로 섞은 가격을 만들어요.
    (거래가 적은 업종 ETF 대신 대표 회사 몇 곳으로 업종을 계산할 때 써요)"""
    parts = [prices[t] for t in ticker.split("|") if t in prices]
    if not parts:
        return None
    df = pd.concat(parts, axis=1, sort=True).ffill(limit=5)
    r = df.pct_change(fill_method=None).mean(axis=1, skipna=True).fillna(0)
    first = df.dropna(how="all").index[0]
    r = r[r.index >= first]
    return 100 * (1 + r).cumprod()


def demo_prices(tickers, seed=11):
    """인터넷 없이 테스트할 때 쓰는 가짜 가격 (업종마다 1등 시기가 바뀌게)."""
    rng = np.random.default_rng(seed)
    out = {}
    idx = pd.bdate_range(end=datetime.now(KST).date(), periods=252 * 22)
    regimes = rng.integers(0, 40, size=len(idx) // 120 + 1)
    for i, t in enumerate(tickers):
        start = 0 if t.startswith("^") or t == GOLD else int(rng.integers(0, 252 * 15))
        r = rng.normal(0.0002, 0.012, len(idx))
        boost = np.repeat(regimes == (i % 40), 120)[: len(idx)] * 0.0025
        r = r + boost
        r[-60:] -= 0.0015 if i % 3 == 0 else 0
        s = pd.Series(100 * np.exp(np.cumsum(r)), index=idx).iloc[start:]
        out[t] = s
    return out


# ───────────────────────── 2. 계산 ─────────────────────────

def ret(s, n):
    s = s.dropna()
    if len(s) <= n:
        return None
    return float(s.iloc[-1] / s.iloc[-1 - n] - 1)


def iga(word):
    """'이/가' 고르기 (받침 있으면 '이')."""
    ch = word.strip()[-1]
    if "가" <= ch <= "힣":
        return word + ("이" if (ord(ch) - 0xAC00) % 28 else "가")
    if ch.isdigit():
        return word + ("이" if ch in "013678" else "가")
    return word + "가"


def pct(x, digits=1):
    if x is None:
        return "—"
    return f"{x * 100:+.{digits}f}%"


def window_table(market, sectors, prices, bench):
    out = {}
    for key, label, n in WINDOWS:
        b = ret(bench, n)
        if b is None:
            continue
        rows = []
        for sc in sectors:
            s = prices.get(sc["ticker"])
            if s is None:
                continue
            r = ret(s, n)
            if r is None:
                continue
            rows.append({"key": sc["key"], "name": sc["name"], "emoji": sc["emoji"],
                         "cat": sc["cat"], "ret": round(r, 5), "rel": round(r - b, 5)})
        if len(rows) < MIN_SECTORS:
            out[key] = {"label": label, "bench": round(b, 5), "rank": rows,
                        "note": "이 기간을 계산할 만큼 업종 데이터가 오래되지 않았어요."}
            continue
        rows.sort(key=lambda x: x["ret"], reverse=True)
        ew = float(np.mean([x["ret"] for x in rows]))
        up = sum(1 for x in rows if x["ret"] > 0)
        out[key] = {"label": label, "bench": round(b, 5), "ew": round(ew, 5),
                    "up": up, "n": len(rows), "rank": rows,
                    "text": explain(market, label, rows, b, ew, up)}
    return out


def explain(market, label, rows, b, ew, up):
    """숫자를 초등학생도 읽을 수 있는 문장으로."""
    bname = BENCH[market][1]
    top, low = rows[0], rows[-1]
    if top["ret"] > 0:
        hero = (f"{top['emoji']} {top['name']} 업종이 {label} 동안 {pct(top['ret'])} 올라 1등이에요. "
                f"{bname}({pct(b)})보다 {abs(top['ret'] - b) * 100:.1f}%p 더 달렸어요.")
    else:
        hero = (f"{label} 동안 모든 업종이 내렸어요. 그중 {top['emoji']} {top['name']} 업종이 "
                f"{pct(top['ret'])}로 가장 덜 떨어져서 잘 버텼어요.")
    worst = f"가장 힘들었던 업종은 {low['emoji']} {low['name']} ({pct(low['ret'])})예요."
    breadth = f"업종 {len(rows)}개 중 {up}개가 올랐어요."
    gap = b - ew
    if gap > 0.03:
        breadth += (f" 그런데 {bname} 지수는 업종 평균({pct(ew)})보다 더 많이 올랐어요. "
                    "큰 회사 몇 곳이 지수를 끌어올렸다는 뜻이에요.")
    elif gap < -0.03:
        breadth += (f" 그런데 {bname} 지수는 업종 평균({pct(ew)})보다 더 나빴어요. "
                    "덩치 큰 대장 회사 몇 곳이 지수를 끌어내렸다는 뜻이에요.")
    return {"hero": hero, "worst": worst, "breadth": breadth}


def month_ends(idx):
    s = pd.Series(idx, index=idx)
    return list(s.groupby([idx.year, idx.month]).max())


def baton_eras(sectors, prices, bench):
    """매달 말, 최근 6개월 성적 1등 업종을 찾아 '바통 이어달리기'를 만들어요."""
    names = {sc["ticker"]: sc for sc in sectors}
    frame = pd.DataFrame({t: prices[t] for t in names if t in prices}).reindex(bench.index).ffill(limit=5)
    leaders = []
    for d in month_ends(bench.index):
        pos = frame.index.get_loc(d)
        if pos < LEAD_DAYS:
            continue
        past = frame.iloc[pos - LEAD_DAYS]
        now = frame.iloc[pos]
        r = (now / past - 1).dropna()
        if len(r) < MIN_SECTORS:
            continue
        t = r.idxmax()
        leaders.append((d, t, float(r[t])))
    if not leaders:
        return []
    # 연속된 같은 1등을 한 덩어리로
    runs = []
    for d, t, r in leaders:
        if runs and runs[-1]["t"] == t:
            runs[-1]["end"] = d
            runs[-1]["months"] += 1
        else:
            runs.append({"t": t, "start": d, "end": d, "months": 1})
    # 잠깐(MIN_HOLD 미만) 1등은 앞 주자에 붙이기
    merged = []
    for run in runs:
        if merged and run["months"] < MIN_HOLD:
            merged[-1]["end"] = run["end"]
            merged[-1]["months"] += run["months"]
            continue
        if merged and merged[-1]["t"] == run["t"]:
            merged[-1]["end"] = run["end"]
            merged[-1]["months"] += run["months"]
            continue
        merged.append(dict(run))
    eras = []
    for i, run in enumerate(merged):
        sc = names[run["t"]]
        s = frame[run["t"]].dropna()
        seg = s[(s.index >= run["start"] - pd.Timedelta(days=183)) & (s.index <= run["end"])]
        gain = float(seg.iloc[-1] / seg.iloc[0] - 1) if len(seg) > 1 else None
        eras.append({"key": sc["key"], "name": sc["name"], "emoji": sc["emoji"], "cat": sc["cat"],
                     "start": run["start"].strftime("%Y-%m-%d"), "end": run["end"].strftime("%Y-%m-%d"),
                     "months": run["months"], "gain": None if gain is None else round(gain, 4),
                     "live": i == len(merged) - 1})
    return eras


def winters(bench):
    """대표 지수가 최고점보다 20% 넘게 떨어졌던 때(겨울)를 찾아요."""
    s = bench.dropna()
    peak_val, peak_day = s.iloc[0], s.index[0]
    out, cur = [], None
    for d, v in s.items():
        if v >= peak_val:
            if cur:
                cur["end"] = d.strftime("%Y-%m-%d")
                out.append(cur)
                cur = None
            peak_val, peak_day = v, d
            continue
        dd = v / peak_val - 1
        if dd <= WINTER and cur is None:
            cur = {"start": peak_day.strftime("%Y-%m-%d"), "end": None,
                   "trough": d.strftime("%Y-%m-%d"), "low": round(float(dd), 4)}
        if cur and dd < cur["low"]:
            cur["low"], cur["trough"] = round(float(dd), 4), d.strftime("%Y-%m-%d")
    if cur:
        out.append(cur)
    now_dd = float(s.iloc[-1] / s.cummax().iloc[-1] - 1)
    pk = s.idxmax() if s.iloc[-1] < s.max() else s.index[-1]
    return out, {"dd": round(now_dd, 4), "peak_date": pk.strftime("%Y-%m-%d"),
                 "peak": round(float(s.max()), 2), "last": round(float(s.iloc[-1]), 2)}


def season_text(market, now):
    bname = BENCH[market][1]
    dd = now["dd"]
    if dd <= WINTER:
        return (f"❄️ {iga(bname)} 가장 높았던 {now['peak_date']}보다 {abs(dd) * 100:.0f}% 낮아요. "
                "20% 넘게 떨어져서 지금은 '겨울'이에요.")
    if dd <= CORRECTION:
        return (f"🌧️ {iga(bname)} 가장 높았던 {now['peak_date']}보다 {abs(dd) * 100:.0f}% 낮아요. "
                "10% 넘게 떨어진 '조정' 중이에요. 아직 겨울은 아니에요.")
    if dd > -0.02:
        return f"☀️ {iga(bname)} 가장 높은 곳 근처에 있어요. 따뜻한 날씨예요."
    return f"🌤️ {iga(bname)} 최고점보다 {abs(dd) * 100:.0f}% 아래예요. 큰 겨울은 아니에요."


# ───────────────────────── 3. 알림 ─────────────────────────

def load_state():
    try:
        with open(os.path.join(DOCS, "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def phone_message(data, focus, changes):
    lines = []
    for m, old, new in changes:
        lines.append(f"🏃 바통이 넘어갔어요! {FLAG[m]}: {old} → {new}")
    order = (["us", "kr", "jp", "tw", "hk", "cn", "sg"] if focus == "US"
             else ["kr", "jp", "tw", "hk", "cn", "sg", "us"])
    for m in order:
        md = data["markets"].get(m)
        if not md or "d1" not in md["windows"]:
            continue
        d1, m1 = md["windows"]["d1"], md["windows"].get("m1")
        line = f"{FLAG[m]} {md['bench_name']} {pct(d1['bench'])}"
        if d1.get("rank"):
            t = d1["rank"][0]
            line += f" · 오늘 1등 {t['emoji']}{t['name']} {pct(t['ret'])}"
        if m1 and m1.get("rank"):
            t = m1["rank"][0]
            line += f" · 1달 주인공 {t['emoji']}{t['name']}"
        if md["now"]["dd"] <= WINTER:
            line += f" · ❄️겨울(고점 대비 {pct(md['now']['dd'], 0)})"
        lines.append(line)
    return "\n".join(lines)


def send_ntfy(title, message):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC 이 없어서 알림은 건너뜁니다.")
        return
    payload = {"topic": topic, "title": title, "message": message, "tags": ["runner"],
               "click": DASH_URL,
               "actions": [{"action": "view", "label": "대시보드 열기", "url": DASH_URL}]}
    r = requests.post("https://ntfy.sh/", data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, timeout=20)
    print("ntfy 전송:", r.status_code)


# ───────────────────────── 4. 실행 ─────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="가짜 데이터로 테스트")
    ap.add_argument("--no-notify", action="store_true", help="휴대폰 알림 보내지 않기")
    args = ap.parse_args()

    sectors = load_sectors()
    singles = {t for s in sectors for t in s["ticker"].split("|")}
    tickers = sorted(singles | {b[0] for b in BENCH.values()} | {GOLD})
    prices = demo_prices(tickers) if args.demo else fetch_prices(tickers)
    for s in sectors:                      # 여러 회사를 묶은 업종 만들기
        if "|" in s["ticker"]:
            b = basket(prices, s["ticker"])
            if b is not None:
                prices[s["ticker"]] = b

    now = datetime.now(KST)
    data = {"generated": now.strftime("%Y-%m-%d %H:%M"), "demo": args.demo, "markets": {}}
    missing_all = []
    for m, (bt, bname) in BENCH.items():
        bench = prices.get(bt)
        if bench is None or len(bench) < 30:
            print(f"{FLAG[m]}: 대표 지수({bt})를 못 받아서 건너뜁니다.")
            continue
        secs = [s for s in sectors if s["market"] == m]
        missing = [f"{s['name']}({s['ticker']})" for s in secs if s["ticker"] not in prices]
        missing_all += missing
        wins = window_table(m, secs, prices, bench)
        w_list, now_state = winters(bench)
        eras = baton_eras(secs, prices, bench)
        data["markets"][m] = {
            "bench_name": bname, "bench_ticker": bt,
            "last_date": bench.index[-1].strftime("%Y-%m-%d"),
            "windows": wins, "eras": eras, "winters": w_list, "now": now_state,
            "season": season_text(m, now_state), "missing": missing,
            "since": min((prices[s["ticker"]].index[0] for s in secs if s["ticker"] in prices),
                         default=bench.index[0]).strftime("%Y-%m-%d"),
        }
    g = prices.get(GOLD)
    if g is not None:
        data["gold"] = {k: ret(g, n) for k, _, n in WINDOWS[:5]}
    dates = [md["last_date"] for md in data["markets"].values()]
    data["asof"] = max(dates) if dates else now.strftime("%Y-%m-%d")
    if missing_all:
        print("가격을 못 받은 업종:", ", ".join(missing_all))

    # 바통 변화 확인 (6개월 1등이 바뀌었나)
    state = load_state()
    changes = []
    new_state = {}
    for m, md in data["markets"].items():
        if not md["eras"]:
            continue
        cur = md["eras"][-1]
        new_state[m] = cur["key"]
        old_key = state.get(m)
        if old_key and old_key != cur["key"] and len(md["eras"]) > 1:
            prev = md["eras"][-2]
            changes.append((m, f"{prev['emoji']}{prev['name']}", f"{cur['emoji']}{cur['name']}"))

    focus = os.environ.get("NOTIFY_FOCUS", "ASIA")
    msg = phone_message(data, focus, changes)
    data["phone"] = msg

    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    if not args.demo:
        with open(os.path.join(DOCS, "state.json"), "w", encoding="utf-8") as f:
            json.dump(new_state, f, ensure_ascii=False)
    print(msg)

    if not args.no_notify and not args.demo and msg:
        title = f"🏃 이어달리기 봇 · {now:%m/%d}" + (" · 바통 넘어감!" if changes else "")
        send_ntfy(title, msg)


if __name__ == "__main__":
    main()
