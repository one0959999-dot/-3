"""라이브 v1.0 드라이런 러너 (EC2용) — 주문 없이 목표 포트폴리오 산출·텔레그램 보고.

★확정 구조 (검증: 2015~2026.6 CAGR 15.5% / MDD -32% / 1000만→5,232만):
  50% 코스피200 ETF(069500 KODEX200)  ← 시총가중: 폭등장 포착 담당
  50% v3 저변동성 25종목 동일비중       ← 방어: 하락장 담당
  분기 리밸런스(1/4/7/10월 첫 주) · 타이밍 판단 없음 · 페이퍼 트레이딩(주문 없음)

동작: kr_ticker_cache 전 종목 최근 ~700일 시세(yfinance) → algo_v1.select() 상위 25
  → 예산 1주단위 배분 → 텔레그램. 데이터 무결성 게이트: 이상하면 아무것도 안 한다.
크론(EC2): 30 23 * * 0  (월요일 08:30 KST)

실행: python KR/live_v1.py [--telegram] [--budget 10000000]
"""
import sys, os, sqlite3, time, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import warnings
warnings.filterwarnings('ignore')
import numpy as np
import pandas as pd
import yfinance as yf
from KR.algo_v1 import select, load_financial_flags

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
ETF = '069500'  # KODEX 200 (시총가중 슬리브)
N_SLEEVE = 25
LOOKBACK_DAYS = 700          # 달력일 (200MA+기울기+버퍼 확보)
MIN_TICKERS = 1500           # 무결성 게이트: 수집이 이보다 적으면 중단
BUY_COST = 0.0015


def tg(msg):
    try:
        c = sqlite3.connect(P('lassi.db'), timeout=30)
        r = c.execute("SELECT telegram_token, telegram_chat_id FROM users "
                      "WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone()
        c.close()
        if r:
            from base.telegram_bot import TelegramNotifier
            TelegramNotifier(r[0], r[1]).send_message(msg)
            return True
    except Exception as e:
        print('텔레그램 실패:', e)
    return False


def fetch_universe():
    c = sqlite3.connect(P('lassi.db'))
    tickers = [t for (t,) in c.execute('SELECT ticker FROM kr_ticker_cache')
               if t and len(t) == 6 and t.isdigit()]
    names = {t: n for t, n in c.execute('SELECT ticker, name FROM kr_ticker_cache')}
    c.close()
    start = (datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()
    cl = {}
    def batch(tks, sfx):
        try:
            data = yf.download([t + sfx for t in tks], start=start, progress=False,
                               auto_adjust=True, threads=True, group_by='ticker')
        except Exception:
            return
        for t in tks:
            try:
                s = (data if len(tks) == 1 else data[t + sfx])['Close'].dropna()
                if len(s) >= 260:
                    cl[t] = s
            except Exception:
                continue
    todo = [ETF] + tickers
    for i in range(0, len(todo), 120):
        batch(todo[i:i + 120], '.KS')
    rem = [t for t in todo if t not in cl]
    for i in range(0, len(rem), 120):
        batch(rem[i:i + 120], '.KQ')
    return cl, names


def is_rebalance_week(d=None):
    d = d or datetime.date.today()
    return d.month in (1, 4, 7, 10) and d.day <= 7


def main(telegram=False, budget=10_000_000):
    t0 = time.time()
    cl, names = fetch_universe()
    # ── 무결성 게이트: 데이터 이상하면 아무것도 안 한다 ──
    if len(cl) < MIN_TICKERS or ETF not in cl:
        msg = f"⛔ live_v1 중단: 데이터 무결성 실패 (수집 {len(cl)}종목, ETF {'OK' if ETF in cl else '누락'}) — 아무것도 하지 않음"
        print(msg); tg(msg); return 1
    etf = cl.pop(ETF)
    panel = pd.DataFrame(cl).sort_index()
    last_day = panel.index[-1].date()
    if (datetime.date.today() - last_day).days > 7:
        msg = f"⛔ live_v1 중단: 최신 데이터가 {last_day} (7일+ 낡음) — 아무것도 하지 않음"
        print(msg); tg(msg); return 1
    fin, fy = load_financial_flags()
    picks = select(panel, fin, fy)[:N_SLEEVE]
    if len(picks) < 15:
        msg = f"⛔ live_v1 중단: 선정 {len(picks)}종목(<15) — 규칙 통과 종목 부족, 점검 필요"
        print(msg); tg(msg); return 1
    # ── 목표 포트폴리오 (1주 단위) ──
    half = budget / 2
    etf_px = float(etf.iloc[-1]); etf_q = int(half // (etf_px * (1 + BUY_COST)))
    per = half / len(picks)
    rebal = is_rebalance_week()
    if rebal:
        L = [f"🔴 [분기 리밸런스] {last_day} — 지금 아래 구성으로 매매하세요 (다음 변경: 3개월 후)"]
    else:
        L = [f"🟢 [유지 주간] {last_day} — 매매 불필요. 현재 보유 그대로 유지하세요.",
             f"    (실제 종목 변경은 분기 1회: 1·4·7·10월. 아래는 참고용 현재 순위)"]
    L.append(f"예산 {budget/1e4:,.0f}만 · 구성 = 지수ETF 50% + 저변동 {len(picks)}종목 50%")
    L.append("")
    L.append(f"[슬리브A 50%] KODEX200({ETF}) {etf_q}주 × {etf_px:,.0f}원 = {etf_q*etf_px/1e4:,.0f}만")
    L.append(f"[슬리브B 50%] 저변동성 {len(picks)}종목 (종목당 ~{per/1e4:,.0f}만):")
    used = 0
    for i, t in enumerate(picks, 1):
        px = float(panel[t].dropna().iloc[-1])
        q = int(per // (px * (1 + BUY_COST)))
        used += q * px
        L.append(f"  {i:2}. {names.get(t, t)[:8]:8} {q}주 × {px:,.0f}")
    L.append("")
    L.append(f"체결가능 배분: ETF {etf_q*etf_px/1e4:,.0f}만 + 종목 {used/1e4:,.0f}만 / 잔여현금 {(budget-etf_q*etf_px-used)/1e4:,.0f}만")
    L.append(f"⚠️ 페이퍼 트레이딩 — 실주문 없음. ({time.time()-t0:.0f}s, {len(panel.columns)}종목 스캔)")
    rep = "\n".join(L)
    print(rep)
    if telegram:
        print('텔레그램', '✓' if tg(rep) else '✗')
    return 0


if __name__ == '__main__':
    b = 10_000_000
    if '--budget' in sys.argv:
        b = int(sys.argv[sys.argv.index('--budget') + 1])
    sys.exit(main('--telegram' in sys.argv, b))
