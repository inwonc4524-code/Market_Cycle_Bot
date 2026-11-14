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
import re
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
# 금값(달러)을 그 나라 돈으로 바꿀 때 쓰는 환율 (1달러 = 몇 원/엔/…)
FX = {"kr": "KRW=X", "jp": "JPY=X", "tw": "TWD=X", "hk": "HKD=X", "cn": "CNY=X", "sg": "SGD=X"}
GOLD_START = 0.10   # 링 그래프: 처음 해에 돈의 10%를 금으로 들고 있었다고 가정

WINDOWS = [  # (키, 이름, 거래일 수)
    ("d1", "1일", 1), ("w1", "1주일", 5), ("m1", "1달", 21), ("m6", "6개월", 126),
    ("y1", "1년", 252), ("y3", "3년", 756), ("y5", "5년", 1260),
    ("y10", "10년", 2520), ("y20", "20년", 5040),
]
LEAD_DAYS = 126     # 바통 판단: 최근 6개월 성적 1등
SHORT_CHECK = {"d1": 0.30, "w1": 0.60}
QUARTER_CHECK = 3.0   # 분기: +300% 넘거나 -90% 넘게 움직인 업종은 데이터 오류로 보고 빼기   # 1일·1주일에 이보다 크게 움직이면 데이터 오류로 보고 빼기
JOIN_WAIT = 60                 # 묶음에 새 회사를 넣기까지 기다리는 거래일 수
MIN_HOLD = 3        # 3달 이상 1등을 지켜야 '바통을 잡았다'고 봐요 (잠깐 1등은 무시)
MIN_SECTORS = 3     # 업종이 3개 이상 있을 때부터 계산
WINTER = -0.20      # 최근 5년 최고점보다 20% 넘게 하락 = 겨울
PEAK_DAYS = 1260    # 겨울 판단 기준: 최근 5년(약 1,260 거래일) 안의 최고점
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
    top_up_stale(out)
    return out


def _grp(t):
    """같은 시장끼리 묶기 (끝 글자 .KS/.T 등, 지수 ^, 없으면 미국)"""
    if t.startswith("^"):
        return "^"
    return t.rsplit(".", 1)[1] if "." in t else "US"


def top_up_stale(out):
    """한꺼번에 받으면 야후가 몇몇 종목의 최신 날을 빼고 줄 때가 있어요
    (2026-11-14: 미국 XLK·XLC·XLU·XLRE 가 하루 전 값이라 1일·1주 순위에서 빠짐).
    같은 시장의 다른 종목보다 최신 날이 이른 종목은 하나씩 최근 1달을 다시 받아 가격 비율로 이어 붙여요."""
    import yfinance as yf
    last = {t: s.index[-1] for t, s in out.items() if len(s)}
    newest = {}
    for t, d in last.items():
        g = _grp(t)
        newest[g] = max(newest.get(g, d), d)
    fixed, left = [], []
    for t, d in last.items():
        g = _grp(t)
        if g == "^" or d >= newest[g]:
            continue
        try:
            r = yf.download(t, period="1mo", auto_adjust=True, progress=False)
            s2 = _close(r, t, False)
            new = s2[s2.index > d]
            if len(new) and d in s2.index and float(s2.loc[d]) > 0:
                out[t] = pd.concat([out[t], new * (float(out[t].iloc[-1]) / float(s2.loc[d]))])
                fixed.append(t)
            else:
                left.append(t)
        except Exception:
            left.append(t)
    if fixed:
        print(f"최신 날이 빠진 종목을 다시 받아 채움 {len(fixed)}: {', '.join(fixed[:20])}")
    if left:
        print(f"다시 받아도 최신 날이 없는 종목 {len(left)}: {', '.join(left[:20])}")


# 장이 아직 안 끝난 나라는 오늘 가격(장 중 가격)을 빼고 어제 마감까지만 써요.
# (나라마다 장 마감 뒤에 봇을 한 번씩 돌리기 때문에, 다른 나라는 아직 장 중일 수 있어요)
CLOSE_AT = {"kr": ("Asia/Seoul", 15, 30), "jp": ("Asia/Tokyo", 15, 30), "tw": ("Asia/Taipei", 13, 30),
            "hk": ("Asia/Hong_Kong", 16, 0), "cn": ("Asia/Shanghai", 15, 0), "sg": ("Asia/Singapore", 17, 0),
            "us": ("America/New_York", 16, 0)}


def drop_spikes(s, jump=1.6, back=0.2):
    """하루만 크게 튀었다가 다음 날 원래대로 돌아오는 값(데이터 오류)을 빼요.
    예) 일본 종합상사 ETF(1629.T)의 2026-03-31 값이 평소의 약 1/400로 들어와 분기 수익률이 -99.8%·+44,000%로 나왔어요.
    조건: 전날→그날, 그날→다음 날이 둘 다 60% 넘게 반대 방향으로 움직이고, 전날과 다음 날은 20% 안쪽으로 비슷할 때.
    돌려주는 값: (고친 가격, 뺀 날짜 목록)"""
    x = s.dropna()
    if len(x) < 3:
        return s, []
    up = x / x.shift(1)
    dn = x.shift(-1) / x
    same = (x.shift(-1) / x.shift(1) - 1).abs() < back
    big = lambda r: (r > jump) | (r < 1 / jump)
    bad = big(up) & big(dn) & ((up > 1) != (dn > 1)) & same
    days = list(x.index[bad.fillna(False)])
    if not days:
        return s, []
    return s.drop(days), days


def trim_open_sessions(prices, sectors):
    """마감 10분 뒤가 안 된 나라의 '오늘' 가격은 빼요. 뺀 나라 목록을 돌려줘요."""
    from zoneinfo import ZoneInfo
    mk = {b[0]: m for m, b in BENCH.items()}
    for s in sectors:
        for t in s["ticker"].split("|"):
            mk.setdefault(t, s["market"])
    cut = []
    for m, (tz, hh, mm) in CLOSE_AT.items():
        now = datetime.now(ZoneInfo(tz))
        done = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(minutes=10)
        if now.weekday() < 5 and now < done:
            today = pd.Timestamp(now.date())
            n = 0
            for t, ser in list(prices.items()):
                if mk.get(t) == m and len(ser) and pd.Timestamp(ser.index[-1]).normalize() == today:
                    prices[t] = ser.iloc[:-1]
                    n += 1
            if n:
                cut.append(f"{FLAG[m]}({n})")
    if cut:
        print("아직 장 중이라 오늘 가격을 뺀 나라:", ", ".join(cut))
    return cut


def daily_limit(t):
    """하루에 움직일 수 있는 최대 폭(가격제한폭). 넘으면 데이터 오류로 봐요.
    (2026-11-08 배움: 중국 의약·바이오 20년 수익률이 503배로 부풂 — 무상증자 보정 오류)"""
    if t.endswith(".SS") or t.endswith(".SZ"):
        code = t.split(".")[0]
        return 0.205 if code.startswith(("300", "301", "688")) else 0.105   # 기술주 시장 ±20%, 일반 ±10%
    if t.endswith(".TW"):
        return 0.105                                                        # 대만 ±10%
    return 0.6


def bad_series(s, t):
    """야후 가격이 망가진 회사 찾기: 하루에 크게 튀었다가 며칠 안에 되돌아오는 일이 여러 번 있거나,
    아주 큰 하루 움직임이 너무 자주 있으면 데이터 오류로 봐요 (예: 2026-11 싱가포르 타이베브 20년 +12억 배)."""
    if daily_limit(t) < 0.6:      # 중국·대만은 가격제한폭으로 이미 걸러져서 따로 빼지 않아요
        return False
    s = s.dropna()
    if len(s) < 30:
        return False
    r = s.pct_change().iloc[5:]
    lim = 0.31 if t.endswith((".KS", ".KQ")) else max(min(daily_limit(t), 0.31), 0.25)
    big = r.abs() > lim + 0.02
    rev = 0
    vals = r.values
    for i in range(len(vals) - 1):
        if abs(vals[i]) > lim + 0.02:
            nxt = vals[i + 1:i + 4]
            if len(nxt) and ((vals[i] > 0 and nxt.min() < -0.18) or (vals[i] < 0 and nxt.max() > 0.18)):
                rev += 1
    return rev >= 3 or int(big.sum()) >= 12


def basket(prices, ticker):
    """'A|B|C' 처럼 여러 회사를 묶은 업종의 가격을 만들어요.
    처음에 똑같은 돈을 나눠 넣고 그대로 들고 있는 방식(사서 보유)으로 계산해서,
    가격 데이터에 하루짜리 오류가 있어도 결과가 크게 틀어지지 않게 했어요.
    하루에 60% 넘게 움직인 값은 데이터 오류로 보고 0으로 처리해요.
    중간에 상장한 회사는 그날의 업종 평균 금액만큼 새로 넣어요 (계단 없이 이어 붙임)."""
    parts = {t: prices[t] for t in ticker.split("|") if t in prices}
    if not parts:
        return None
    df = pd.concat(parts, axis=1, sort=True).ffill(limit=5)
    r = df.pct_change(fill_method=None)
    for t in df.columns:          # 가격제한폭을 넘는 움직임은 오류로 빼기 (상장 첫 5거래일은 제외)
        lim = daily_limit(t)
        first = df[t].first_valid_index()
        if first is None:
            continue
        pos = df.index.get_loc(first)
        mask = r[t].abs() > lim
        mask.iloc[: pos + 6] = False
        r.loc[mask, t] = 0.0
    r = r.where(r.abs() <= 0.6, 0.0)
    level = 100.0
    hold2 = {}
    out = []
    # 새로 상장한 회사는 상장 후 60거래일이 지난 뒤에 넣어요.
    # (중국은 상장 직후 몇 주 동안 상한가가 계속되는데, 그 값은 실제로 살 수 없는 가격이라 수익이 부풀려져요)
    ready = {}
    firsts = {t: df[t].first_valid_index() for t in df.columns}
    start0 = min(f for f in firsts.values() if f is not None)
    for t, first in firsts.items():
        if first is None:
            continue
        if first <= start0 + pd.Timedelta(days=10):   # 처음부터 있던 회사는 바로 넣기
            ready[t] = first
            continue
        pos = df.index.get_loc(first)
        ready[t] = df.index[min(pos + JOIN_WAIT, len(df.index) - 1)]
    for d in df.index:
        row = df.loc[d]
        before = sum(hold2.values())
        for t in list(hold2):
            x = r.at[d, t]
            if pd.notna(x):
                hold2[t] *= 1 + x
        after = sum(hold2.values())
        if before > 0:
            level *= after / before
        for t in df.columns:
            if t not in hold2 and pd.notna(row[t]) and d >= ready.get(t, d):
                hold2[t] = (after / len(hold2)) if hold2 else 1.0
        out.append(level if hold2 else np.nan)
    return pd.Series(out, index=df.index).dropna()


def cap_list(sc):
    """업종 덩치(시가총액)를 잴 때 쓰는 대표 회사들."""
    if sc.get("caps"):
        return [t for t in sc["caps"].split("|") if t]
    return [t for t in sc["ticker"].split("|") if t]


def fetch_raw_and_caps(tickers):
    """배당을 반영하지 않은 가격(덩치 계산용)과 지금 시가총액을 받아와요."""
    import yfinance as yf
    from concurrent.futures import ThreadPoolExecutor

    raw = {}
    data = yf.download(tickers, period="max", auto_adjust=False, progress=False,
                       group_by="ticker", threads=True)
    for t in tickers:
        try:
            s = _close(data, t, len(tickers) > 1)
            if len(s):
                raw[t] = s
        except Exception:
            pass

    def one(t):
        cap, shares, splits = None, None, None
        try:
            tk = yf.Ticker(t)
            try:
                cap = getattr(tk.fast_info, "market_cap", None) or tk.info.get("marketCap")
            except Exception:
                cap = None
            if not t.endswith("=X"):
                try:
                    shares = tk.get_shares_full(start="1995-01-01")
                except Exception:
                    shares = None
                try:
                    splits = tk.splits
                except Exception:
                    splits = None
        except Exception:
            pass
        return t, (float(cap) if cap else None), shares, splits

    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, tickers))
    caps = {t: c for t, c, _, _ in res if c}
    hist = {t: (s, sp) for t, _, s, sp in res if s is not None and len(s) >= 2}
    return raw, caps, hist


def demo_raw_and_caps(tickers):
    rng = np.random.default_rng(5)
    raw = demo_prices(tickers, seed=5)
    caps = {t: float(rng.lognormal(23, 1.2)) for t in raw}
    hist = {}
    for t in list(raw)[::3]:          # 일부 회사만 '과거 주식 수'가 있다고 가정
        idx = raw[t].index[::60]
        hist[t] = (pd.Series(np.linspace(0.6, 1.0, len(idx)) * 1e9, index=idx), pd.Series(dtype=float))
    return raw, caps, hist


def _naive(idx):
    return pd.to_datetime(idx, utc=True).tz_convert(None).normalize()


def align_splits(sh, sp, window_days=1100, tol=0.3):
    """과거 주식 수 기록을 '지금(분할 후) 기준'으로 맞춰요.
    야후 기록은 분할 전 숫자와 분할 후 숫자가 섞여 있을 때가 있어서,
    무조건 곱하지 않고 '기록이 실제로 분할 비율만큼 뛴 곳'만 찾아서 맞춰요.
    (2026-11-08 배움: 삼성전자 2018년 50:1 분할 때 무조건 ×50 하다가 2017년 반도체가 87%로 부풂.
     기록이 바뀐 곳이 분할 날짜와 1년 넘게 떨어져 있을 수도 있어서 3년 안을 찾아요)"""
    if len(sh) < 2:
        return sh
    if sp is not None and len(sp):
        sp = pd.Series(sp.values, index=_naive(sp.index)).astype(float)
        sp = sp[sp > 0]
        sp = sp[(sp > 1.05) | (sp < 0.95)]
    else:
        sp = pd.Series(dtype=float, index=pd.DatetimeIndex([]))
    vals = sh.values.astype(float).copy()
    idx = sh.index
    for i in range(len(vals) - 1, 0, -1):               # 최근 → 과거로 가면서
        ratio = vals[i] / vals[i - 1]                    # (나중 기록) ÷ (이전 기록)
        if 0.625 < ratio < 1.6:
            continue
        lr = abs(np.log(ratio))
        near = sp[(abs((sp.index - idx[i]).days) <= window_days)] if len(sp) else sp
        if any(abs(lr - abs(np.log(r))) < tol for r in near.values):
            vals[:i] *= ratio                            # 이전 기록을 나중 기준으로 맞춤
        # 분할과 맞지 않는 큰 변화는 진짜 주식 수 변화(대규모 유상증자·합병)로 보고 그대로 둬요.
        # (2026-11-08 배움: '8배 넘으면 기록 단위 변화'로 봤더니 셈코프마린(시트리움)의
        #  2020년 15배 유상증자를 지워 버려서 싱가포르 조선·해양이 30%로 부풂)
    return pd.Series(vals, index=idx)


def cap_series(price, cap_now, hist=None):
    """회사의 과거 덩치(시가총액) 줄을 만들어요.
    · 과거 주식 수가 있으면: 그날 주가 × 그날 주식 수 (액면분할은 맞춰서 보정)
    · 없으면: 지금 덩치 × (그날 주가 ÷ 지금 주가)
    마지막 날 값은 지금 시가총액에 맞춰요."""
    price = price.dropna()
    if not len(price):
        return None, False
    if hist is not None:
        try:
            sh, sp = hist
            sh = pd.Series(sh.values, index=_naive(sh.index)).astype(float)
            sh = sh[sh > 0]
            sh = sh[~sh.index.duplicated(keep="last")].sort_index()
            med = sh.rolling(7, center=True, min_periods=1).median()
            sh = sh[(sh / med).between(0.5, 2.0)]          # 튀는 값(데이터 오류) 빼기
            sh = align_splits(sh, sp)                       # 액면분할 전후 숫자 맞추기
            if len(sh) >= 2:
                full = sh.reindex(price.index.union(sh.index)).sort_index().ffill().bfill()
                cap = price * full.reindex(price.index)
                if cap_now and cap.iloc[-1] > 0:
                    cap = cap * (cap_now / cap.iloc[-1])
                    # 안전장치: 주식 수 기록으로 계산한 옛날 덩치가 '주가 변화로만 계산한 덩치'와
                    # 10배 넘게 다르면 기록이 엉킨 것으로 보고 믿지 않아요.
                    # (2026-11-08 배움: 시트리움 2005년 덩치가 750억 싱가포르달러로 나옴)
                    simple = price / price.iloc[-1] * cap_now
                    r = (cap / simple).replace([np.inf, -np.inf], np.nan).dropna()
                    if len(r) and (r.max() > 10 or r.min() < 0.1):
                        return simple, False
                return cap, True
        except Exception:
            pass
    if cap_now:
        return price / price.iloc[-1] * cap_now, False
    return None, False


def yearly_mix(secs, capser, bench, gold_local):
    """해마다(그해 마지막 거래일) 업종별 덩치 비중 + 금 칸을 계산해요.
    회사 덩치(과거) = 지금 시가총액 × (그때 주가 ÷ 지금 주가) — 주식 수 변화는 무시한 대략값."""
    sector_caps = {}
    since = {sc["key"]: int(sc["since"]) for sc in secs if sc.get("since")}
    used = with_hist = 0
    for sc in secs:
        parts = []
        for t in cap_list(sc):
            if t not in capser:
                continue
            s, ok = capser[t]
            used += 1
            with_hist += ok
            parts.append(s.rename(t))
        if parts:
            sector_caps[sc["key"]] = pd.concat(parts, axis=1, sort=True).ffill(limit=10)
    if len(sector_caps) < MIN_SECTORS or gold_local is None:
        return None
    b = bench.dropna()
    g = gold_local.dropna()
    years, base = [], None
    for y in sorted(set(b.index.year)):
        d = b[b.index.year == y].index[-1]
        gw = g[g.index <= d]
        if not len(gw) or (d - gw.index[-1]).days > 10:
            continue
        w = {}
        for k, df in sector_caps.items():
            if since.get(k) and y < since[k]:   # 그 업종(테마)이 아직 없던 해는 빼기
                continue
            sub = df[df.index <= d]
            if len(sub):
                v = sub.iloc[-1].sum(min_count=1)
                if pd.notna(v) and v > 0:
                    w[k] = float(v)
        live = sum(1 for sc in secs if not since.get(sc["key"]) or y >= since[sc["key"]])
        if len(w) < max(MIN_SECTORS, 0.75 * live):   # 그해에 있던 업종 대부분(75%)이 생긴 해부터
            continue
        if base is None:
            base = (float(b[d]), float(gw.iloc[-1]))
        br, gr = float(b[d]) / base[0], float(gw.iloc[-1]) / base[1]
        gs = GOLD_START * gr / ((1 - GOLD_START) * br + GOLD_START * gr)
        tot = sum(w.values())
        years.append({"y": int(y), "date": d.strftime("%Y-%m-%d"), "gold": round(gs, 4),
                      "w": {k: round(v / tot * (1 - gs), 4) for k, v in w.items()}})
    if not years:
        return None
    if years[-1]["y"] == datetime.now(KST).year:
        years[-1]["now"] = True
    # 점검용 경고: 한 해 만에 한 업종 조각이 2.5배 넘게, 15%p 넘게 커지거나 작아지면 표시
    warnings = []
    for a_, b_ in zip(years, years[1:]):
        for k in set(a_["w"]) & set(b_["w"]):
            x, y2 = a_["w"][k], b_["w"][k]
            if abs(y2 - x) > 0.15 and (y2 > 2.5 * x or x > 2.5 * y2):
                df = sector_caps[k]
                comp = {}
                for t in df.columns:          # 원인 찾기용: 회사별 덩치(그해 말)
                    v0 = df[t][df.index <= a_["date"]].dropna()
                    v1 = df[t][df.index <= b_["date"]].dropna()
                    comp[t] = [round(float(v0.iloc[-1]) / 1e9, 2) if len(v0) else None,
                               round(float(v1.iloc[-1]) / 1e9, 2) if len(v1) else None]
                warnings.append({"year": b_["y"], "sector": k, "from": round(x, 4), "to": round(y2, 4),
                                 "companies_bn": comp})
    return {"base": years[0]["y"], "gold_start": GOLD_START,
            "share_hist": round(with_hist / used, 2) if used else 0,
            "warnings": warnings,
            "sectors": {sc["key"]: {"name": sc["name"], "emoji": sc["emoji"]} for sc in secs},
            "years": years}


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


# 1주일·1달은 '거래일 개수'가 아니라 달력 날짜로 시작일을 정해요 (2026-11-14 1주일 돌아보기).
# 휴장일이 끼면 5거래일 전이 지난주 금요일보다 더 앞(예: 싱가포르 11/9 휴장 → 11/5)이 되어
# 뉴스의 '한 주 등락률'과 크게 달라졌어요. 이제 '마지막 날 − 7일(1달은 − 1달)' 또는 그 전 마지막 장이 시작일이에요.
CAL_WINDOWS = {"w1": pd.DateOffset(days=7), "m1": pd.DateOffset(months=1)}


def cal_start(s, key):
    """달력 기준 시작일 (그날이 휴장이면 그 전 마지막 장). 없으면 None."""
    s = s.dropna()
    if not len(s):
        return None
    prior = s.loc[:s.index[-1] - CAL_WINDOWS[key]]
    return prior.index[-1] if len(prior) else None


def ret_from(s, start):
    """start 날(또는 그 전 마지막 장) 마감부터 마지막 날까지 등락률."""
    s = s.dropna()
    if start is None or not len(s):
        return None
    prior = s.loc[:start]
    if not len(prior) or s.index[-1] <= prior.index[-1]:
        return None
    return float(s.iloc[-1] / prior.iloc[-1] - 1)


def ret_win(s, key, n):
    """창(key)에 맞는 등락률: 1주일·1달은 달력 기준, 나머지는 거래일 개수."""
    if key in CAL_WINDOWS:
        return ret_from(s, cal_start(s, key))
    return ret(s, n)


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
        b = ret_win(bench, key, n)
        if b is None:
            continue
        bstart = cal_start(bench, key) if key in CAL_WINDOWS else None
        rows = []
        skipped = []
        bend = bench.dropna().index[-1]
        for sc in sectors:
            s = prices.get(sc["ticker"])
            if s is None:
                continue
            s = s.loc[:bend]          # 지수와 같은 날까지만 비교 (지수 값이 늦게 들어온 날 대비)
            r = ret_from(s, bstart) if bstart is not None else ret(s, n)   # 1주일·1달은 지수와 같은 시작일
            if r is None:
                continue
            # 짧은 기간(1일·1주일) 자동 검사: 지수와 마지막 날짜가 다르거나, 하루에 너무 크게 움직이면 빼기
            if key in SHORT_CHECK:
                send = s.dropna().index[-1]
                if send < bend:
                    skipped.append(f"{sc['name']}(아직 {send.strftime('%m/%d')} 값)")
                    continue
                if abs(r) > SHORT_CHECK[key]:
                    skipped.append(f"{sc['name']}(값이 이상해요 {r:+.0%})")
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
        if skipped:
            out[key]["skipped"] = skipped
    return out


def quarter_list(last):
    """보여줄 분기: '가장 최근에 끝난 분기'가 있는 해의 1분기부터 지금 진행 중인 분기까지.
    예) 2026-11 → 2026년 1·2·3분기 + 4분기(진행 중), 2027-01 → 2026년 1~4분기 + 2027년 1분기(진행 중)"""
    cur = pd.Period(last, freq="Q")
    done = cur - 1
    q = pd.Period(f"{done.year}Q1", freq="Q")
    out = []
    while q <= cur:
        out.append(q)
        q += 1
    return out


def quarter_table(market, sectors, prices, bench):
    """분기별 성적: 앞 분기 마지막 장 마감 → 그 분기 마지막 장 마감(진행 중이면 오늘까지)."""
    b_all = bench.dropna()
    if not len(b_all):
        return []
    last = b_all.index[-1]
    out = []
    for q in quarter_list(last):
        qs, qe = q.start_time.normalize(), q.end_time.normalize()
        b0s = b_all.loc[:qs - pd.Timedelta(days=1)]
        b1s = b_all.loc[:qe]
        if not len(b0s) or not len(b1s) or b1s.index[-1] < qs:
            continue
        d0, d1 = b0s.index[-1], b1s.index[-1]
        b = float(b1s.iloc[-1] / b0s.iloc[-1] - 1)
        rows, skipped = [], []
        for sc in sectors:
            s = prices.get(sc["ticker"])
            if s is None:
                continue
            s = s.dropna().loc[:d1]
            p0 = s.loc[:d0]
            if not len(p0) or not len(s) or s.index[-1] <= p0.index[-1] or (d0 - p0.index[-1]).days > 10:
                continue
            if (d1 - s.index[-1]).days > 5:      # 업종 값이 지수보다 많이 늦으면 같은 기간이 아니라서 빼기
                skipped.append(f"{sc['name']}(아직 {s.index[-1].strftime('%m/%d')} 값)")
                continue
            r = float(s.iloc[-1] / p0.iloc[-1] - 1)
            if r > QUARTER_CHECK or r < -0.9:    # 한 분기에 4배 넘게 오르거나 90% 넘게 내리면 데이터 오류로 보고 빼기
                skipped.append(f"{sc['name']}(값이 이상해요 {r:+.0%})")
                continue
            rows.append({"key": sc["key"], "name": sc["name"], "emoji": sc["emoji"],
                         "cat": sc["cat"], "ret": round(r, 5), "rel": round(r - b, 5)})
        # 분기 마지막 날이 휴장이어도(예: 한국·일본 12/31) 달력으로 분기가 지났으면 '끝난 분기'
        today = pd.Timestamp(datetime.now(KST).date())
        partial = bool(last < qe and today <= qe)
        label = f"{q.year}년 {q.quarter}분기" + (" (지금까지)" if partial else "")
        item = {"key": f"{q.year}Q{q.quarter}", "label": label, "partial": partial,
                "start": d0.strftime("%Y-%m-%d"), "end": d1.strftime("%Y-%m-%d"), "bench": round(b, 5)}
        if skipped:
            item["skipped"] = skipped
        if len(rows) >= MIN_SECTORS:
            rows.sort(key=lambda x: x["ret"], reverse=True)
            ew = float(np.mean([x["ret"] for x in rows]))
            up = sum(1 for x in rows if x["ret"] > 0)
            item.update({"ew": round(ew, 5), "up": up, "n": len(rows), "rank": rows,
                         "text": explain(market, label, rows, b, ew, up)})
        else:
            item.update({"rank": rows, "note": "이 분기를 계산할 만큼 업종 데이터가 없어요."})
        out.append(item)
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
    ends = month_ends(bench.index)
    # 아직 끝나지 않은 이번 달(데이터 마지막 날)은 1등 투표에 넣지 않아요.
    # (넣으면 그날그날 1등이 바뀔 때마다 바통이 넘어갔다 돌아왔다 하는 가짜 알림이 생겨요)
    last_day = bench.index[-1]
    if ends and ends[-1] == last_day:
        ends = ends[:-1]
    for d in ends:
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
    if len(merged) > 1 and merged[0]["months"] < MIN_HOLD:   # 데이터 맨 처음의 짧은 칸은 다음 주자에 붙이기
        merged[1]["start"] = merged[0]["start"]
        merged[1]["months"] += merged[0]["months"]
        merged = merged[1:]
    if merged:
        merged[-1]["end"] = last_day        # 지금 달리는 주자는 오늘까지
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
    """대표 지수가 '최근 5년 안의 최고점'보다 20% 넘게 떨어졌던 때(겨울)를 찾아요.
    (아주 오래전 최고점 하나 때문에 몇십 년째 겨울로 나오지 않게, 일곱 나라 모두 같은 잣대)"""
    s = bench.dropna()
    roll = s.rolling(PEAK_DAYS, min_periods=1).max()
    dd = s / roll - 1
    out, cur = [], None
    for d, v in dd.items():
        if v >= -1e-9:
            if cur:
                cur["end"] = d.strftime("%Y-%m-%d")
                out.append(cur)
                cur = None
            continue
        if v <= WINTER and cur is None:
            win = s.loc[:d].iloc[-PEAK_DAYS:]
            cur = {"start": win.idxmax().strftime("%Y-%m-%d"), "end": None,
                   "trough": d.strftime("%Y-%m-%d"), "low": round(float(v), 4)}
        if cur and v < cur["low"]:
            cur["low"], cur["trough"] = round(float(v), 4), d.strftime("%Y-%m-%d")
    if cur:
        out.append(cur)
    win = s.iloc[-PEAK_DAYS:]
    pk = win.idxmax()
    return out, {"dd": round(float(s.iloc[-1] / win.max() - 1), 4), "peak_date": pk.strftime("%Y-%m-%d"),
                 "peak": round(float(win.max()), 2), "last": round(float(s.iloc[-1]), 2)}


def season_text(market, now):
    bname = BENCH[market][1]
    dd = now["dd"]
    if dd <= WINTER:
        return (f"❄️ {iga(bname)} 최근 5년 중 가장 높았던 {now['peak_date']}보다 {abs(dd) * 100:.0f}% 낮아요. "
                "20% 넘게 떨어져서 지금은 '겨울'이에요.")
    if dd <= CORRECTION:
        return (f"🌧️ {iga(bname)} 최근 5년 중 가장 높았던 {now['peak_date']}보다 {abs(dd) * 100:.0f}% 낮아요. "
                "10% 넘게 떨어진 '조정' 중이에요. 아직 겨울은 아니에요.")
    if dd > -0.02:
        return f"☀️ {iga(bname)} 최근 5년 중 가장 높은 곳 근처에 있어요. 따뜻한 날씨예요."
    return f"🌤️ {iga(bname)} 최근 5년 최고점보다 {abs(dd) * 100:.0f}% 아래예요. 큰 겨울은 아니에요."


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


def headline(data, focus):
    """알림 제목용 한 줄: 먼저 보여줄 나라의 지수와 오늘 1등 업종."""
    order = (["us", "kr", "jp", "tw", "hk", "cn", "sg"] if focus == "US"
             else ["kr", "jp", "tw", "hk", "cn", "sg", "us"])
    for m in order:
        md = data["markets"].get(m)
        if not md or "d1" not in md["windows"]:
            continue
        d1 = md["windows"]["d1"]
        head = f"{md['bench_name']} {pct(d1['bench'])}"
        if d1.get("rank"):
            head += f" · {d1['rank'][0]['name']} 1등"
        return FLAG[m], head, d1["bench"]
    return "", "오늘의 시장", 0


def send_ntfy(title, message, card=None, tags=None, priority=3):
    import newscard
    newscard.send(os.environ.get("NTFY_TOPIC"), title, newscard.tidy(message), click=DASH_URL,
                  tags=tags or ["runner"], priority=priority,
                  actions=[{"action": "view", "label": "대시보드 열기", "url": DASH_URL}], card=card)


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
    if not args.demo:
        trim_open_sessions(prices, sectors)
    spikes = {}
    for t in list(prices):
        fixed, days = drop_spikes(prices[t])
        if days:
            prices[t] = fixed
            spikes[t] = [d.strftime("%Y-%m-%d") for d in days]
            print(f"  ! 하루짜리 튀는 값 뺌: {t} {', '.join(spikes[t][:5])}{' …' if len(days) > 5 else ''}")
    # 데이터가 망가진 회사는 계산에서 빼기 (대시보드에 '데이터 오류로 뺀 회사'로 표시)
    bad_data = {}
    for s in sectors:
        if "|" not in s["ticker"]:
            continue
        for t in s["ticker"].split("|"):
            if t in prices and bad_series(prices[t], t):
                bad_data.setdefault(s["market"], set()).add(t)
    for m, ts in bad_data.items():
        for t in ts:
            prices.pop(t, None)
            print(f"  ! 데이터 오류로 뺀 회사: {m} {t}")
    for s in sectors:                      # 여러 회사를 묶은 업종 만들기
        if "|" in s["ticker"]:
            b = basket(prices, s["ticker"])
            if b is not None:
                prices[s["ticker"]] = b

    # 링 그래프용: 대표 회사 덩치 + 환율
    bad_all = set().union(*bad_data.values()) if bad_data else set()
    cap_t = sorted({t for s in sectors for t in cap_list(s)} - bad_all)
    fx_t = sorted(set(FX.values()))
    raw, caps, hist = (demo_raw_and_caps(cap_t + fx_t) if args.demo else fetch_raw_and_caps(cap_t + fx_t))
    capser = {}
    for t in cap_t:
        if t in raw:
            s, ok = cap_series(raw[t], caps.get(t), hist.get(t))
            if s is not None and len(s) > 1:
                capser[t] = (s, ok)
    cap_missing = [t for t in cap_t if t not in capser]
    print(f"덩치를 계산한 회사: {len(capser)}/{len(cap_t)} "
          f"(과거 주식 수로 계산: {sum(ok for _, ok in capser.values())})")

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
        try:
            quarters = quarter_table(m, secs, prices, bench)
        except Exception as e:
            print(f"{FLAG[m]} 분기 계산 실패: {e}")
            quarters = []
        w_list, now_state = winters(bench)
        eras = baton_eras(secs, prices, bench)
        gold_local = None
        if GOLD in prices:
            gold_local = prices[GOLD]
            if m in FX and FX[m] in raw:
                fx = raw[FX[m]].reindex(gold_local.index).ffill()
                gold_local = (gold_local * fx).dropna()
            elif m in FX:
                gold_local = None
        try:
            mix = yearly_mix(secs, capser, bench, gold_local)
        except Exception as e:
            print(f"{FLAG[m]} 링 그래프 계산 실패: {e}")
            mix = None
        data["markets"][m] = {
            "bench_name": bname, "bench_ticker": bt,
            "last_date": bench.index[-1].strftime("%Y-%m-%d"),
            "windows": wins, "quarters": quarters, "eras": eras, "winters": w_list, "now": now_state,
            "season": season_text(m, now_state), "missing": missing, "mix": mix,
            "cap_missing": [t for t in cap_missing if t not in bad_all and any(t in cap_list(s) for s in secs)],
            "bad_data": sorted(bad_data.get(m, [])),
            "since": min((prices[s["ticker"]].index[0] for s in secs if s["ticker"] in prices),
                         default=bench.index[0]).strftime("%Y-%m-%d"),
        }
    # 점검용: 회사 묶음 업종의 회사별 10년·20년 수익률 (이상한 회사 찾기)
    parts = {}
    for s in sectors:
        if "|" in s["ticker"] and s["market"] in data["markets"]:
            row = {}
            for t in s["ticker"].split("|"):
                if t in prices:
                    row[t] = {k: (None if ret(prices[t], n) is None else round(ret(prices[t], n), 2))
                              for k, _, n in WINDOWS if k in ("y5", "y10", "y20")}
                    row[t]["since"] = prices[t].index[0].strftime("%Y-%m-%d")
            bk = prices.get(s["ticker"])
            if bk is not None:
                row["_basket_by_year"] = {str(y): round(float(v), 1) for y, v in bk.groupby(bk.index.year).last().items()}
            parts[f"{s['market']}:{s['key']}"] = row
    data["parts"] = parts
    data["spikes"] = spikes   # 점검용: 하루짜리 튀는 값이라 뺀 날짜 (종목별)
    g = prices.get(GOLD)
    if g is not None:
        data["gold"] = {k: ret_win(g, k, n) for k, _, n in WINDOWS[:5]}
    # 안전장치: 야후가 잠깐 옛날 값을 줄 때(새벽 등) 이미 받은 최신 값을 옛날 값으로 덮어쓰지 않기
    if not args.demo:
        try:
            with open(os.path.join(DOCS, "data.json"), encoding="utf-8") as f:
                old = json.load(f)
            for m, md in list(data["markets"].items()):
                om = (old.get("markets") or {}).get(m)
                if om and om.get("last_date", "") > md["last_date"]:
                    print(f"{m}: 새로 받은 값({md['last_date']})이 이미 있는 값({om['last_date']})보다 옛날이라 그대로 둠")
                    data["markets"][m] = om
        except Exception as e:
            print("이전 data.json 비교 못 함:", e)
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
        flag, head, b = headline(data, focus)
        arrow = "📈" if b > 0 else ("📉" if b < 0 else "➖")
        title = f"{flag} {head}" + (" · 🏃 바통 넘어감!" if changes else "")   # 📈/📉 는 태그가 붙여줘요
        kicker = "바통 교체" if changes else ("미국 장 마감" if focus == "US" else "아시아 장 마감")
        big = abs(b) >= 0.03 or bool(changes)        # 크게 움직였거나 바통이 넘어간 날은 더 눈에 띄게
        card = {"paper": "이어달리기 신문", "kicker": kicker, "headline": head, "lines": [re.sub(r" · 1달 주인공.*", "", x).replace("오늘 1등 ", "1등 ") for x in msg.split("\n")]}
        send_ntfy(title, msg, card=card, tags=["chart_with_upwards_trend" if b >= 0 else "chart_with_downwards_trend"],
                  priority=4 if big else 3)


if __name__ == "__main__":
    main()
