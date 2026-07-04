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
# KR 국내 2슬리브 (미국은 별도 US계좌 전략으로 분리). 검증: 50/50 = CAGR15.5%/MDD-32%.
ETFS = {'069500': 'KODEX200(코스피)'}  # 국내 시총가중 슬리브 (미국ETF 360750 제거 — KR은 국내만)
ETF = '069500'  # 무결성 게이트용
N_SLEEVE = 25
KR_ETF_WEIGHT = 0.5  # ETF 50% + 저변동 50%
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
    todo = list(ETFS) + tickers
    for i in range(0, len(todo), 120):
        batch(todo[i:i + 120], '.KS')
    rem = [t for t in todo if t not in cl]
    for i in range(0, len(rem), 120):
        batch(rem[i:i + 120], '.KQ')
    return cl, names


def is_rebalance_week(d=None):
    d = d or datetime.date.today()
    return d.month in (1, 4, 7, 10) and d.day <= 7


def compute_target(cl, names, budget):
    """KR 국내 50/50 (KODEX200 + v3 저변동) 목표 산출 — 백테스트·라이브·자동주문 공용 단일소스.
    반환 (target, meta) 또는 (None, 사유). target=[{symbol,name,qty,price,sleeve}]."""
    for e in ETFS:
        if e not in cl:
            return None, f"ETF {e} 시세 누락"
    if len(cl) < MIN_TICKERS:
        return None, f"수집 {len(cl)}종목(<{MIN_TICKERS})"
    etf_px = {e: float(cl[e].iloc[-1]) for e in ETFS}
    panel = pd.DataFrame({t: s for t, s in cl.items() if t not in ETFS}).sort_index()
    last_day = panel.index[-1].date()
    if (datetime.date.today() - last_day).days > 7:
        return None, f"최신데이터 {last_day} (7일+ 낡음)"
    fin, fy = load_financial_flags()
    picks = select(panel, fin, fy)[:N_SLEEVE]
    if len(picks) < 15:
        return None, f"선정 {len(picks)}종목(<15)"
    etf_bud = budget * KR_ETF_WEIGHT / max(len(ETFS), 1)
    stock_bud = budget * (1 - KR_ETF_WEIGHT)
    target = []
    for e, nm in ETFS.items():
        target.append({'symbol': e, 'name': nm, 'qty': int(etf_bud // (etf_px[e] * (1 + BUY_COST))),
                       'price': etf_px[e], 'sleeve': '지수ETF'})
    per = stock_bud / len(picks)
    for t in picks:
        px = float(panel[t].dropna().iloc[-1])
        target.append({'symbol': t, 'name': names.get(t, t), 'qty': int(per // (px * (1 + BUY_COST))),
                       'price': px, 'sleeve': '저변동'})
    return target, {'last_day': last_day, 'n_scan': len(panel.columns), 'n_picks': len(picks)}


def main(telegram=False, budget=10_000_000):
    t0 = time.time()
    cl, names = fetch_universe()
    target, meta = compute_target(cl, names, budget)
    if target is None:
        msg = f"⛔ live_v1 중단: 데이터 무결성 실패 ({meta}) — 아무것도 하지 않음"
        print(msg); tg(msg); return 1
    last_day = meta['last_day']
    if is_rebalance_week():
        L = [f"🔴 [분기 리밸런스] {last_day} — 지금 아래 구성으로 매매하세요 (다음 변경: 3개월 후)"]
    else:
        L = [f"🟢 [유지 주간] {last_day} — 매매 불필요. 현재 보유 그대로 유지하세요.",
             f"    (실제 종목 변경은 분기 1회: 1·4·7·10월. 아래는 참고용 현재 순위)"]
    L.append(f"예산 {budget/1e4:,.0f}만 · 구성(국내만) = 코스피ETF 50% + 저변동 {meta['n_picks']}종목 50%")
    L.append("")
    L.append("[슬리브1 = 코스피 지수ETF 50%]")
    for it in [x for x in target if x['sleeve'] == '지수ETF']:
        L.append(f"  {it['name']}({it['symbol']}) {it['qty']}주 × {it['price']:,.0f} = {it['qty']*it['price']/1e4:,.0f}만")
    L.append(f"[슬리브2 = 저변동 {meta['n_picks']}종목 50%]")
    for i, it in enumerate([x for x in target if x['sleeve'] == '저변동'], 1):
        L.append(f"  {i:2}. {it['name'][:8]:8} {it['qty']}주 × {it['price']:,.0f}")
    used = sum(it['qty'] * it['price'] for it in target)
    L.append("")
    L.append(f"체결가능 총액 {used/1e4:,.0f}만 / 잔여현금 {(budget-used)/1e4:,.0f}만")
    L.append(f"⚠️ 페이퍼 트레이딩 — 실주문 없음. ({time.time()-t0:.0f}s, {meta['n_scan']}종목 스캔)")
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
