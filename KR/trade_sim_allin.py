"""봇 매매기법으로 1000만원 한 종목 올인 매매 시뮬 (10년) — 15종목 + 단순보유 비교.

봇: 바닥신호(bottom_score>=2)면 전액매수, 천장신호(top_score>=2)면 전액매도. 1주단위·실거래비용.
비교: 봇매매 최종액 vs 단순보유(시작매수·끝까지) 최종액. 1000만 시작.
실행: python KR/trade_sim_allin.py [--telegram]
"""
import sys, os, pickle, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.detect_trade_score_v2 import feats, bottom_score, top_score, vol_of

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
END = '2025-12-31'; INIT = 10_000_000
BUY_COST = 0.001 + 0.0005; SELL_COST = 0.001 + 0.0005 + 0.0018  # 슬리피지+수수료(+매도세)
STOCKS = ['지수', '005930', '247540', '000660', '035420', '051910', '005380', '068270',
          '105560', '042660', '034020', '196170', '028300', '011200', '012450']


def sim_bot(df):
    d = df[df.index <= END]
    c = d['close']; F = feats(d); vol = vol_of(c)
    bs = bottom_score(F, vol); ts = top_score(F, vol)
    cash = float(INIT); sh = 0
    for i in range(len(c)):
        p = float(c.iloc[i])
        if i < 60 or p <= 0 or np.isnan(p):
            continue
        if sh == 0 and bs.iloc[i] >= 2:                 # 바닥신호 → 전액매수
            q = int(cash // (p * (1 + BUY_COST)))
            if q > 0:
                cash -= q * p * (1 + BUY_COST); sh = q
        elif sh > 0 and ts.iloc[i] >= 2:                # 천장신호 → 전액매도
            cash += sh * p * (1 - SELL_COST); sh = 0
    last = float(c.iloc[-1])
    return cash + sh * last


def sim_hold(df):
    d = df[df.index <= END]; c = d['close']
    p0 = float(c.iloc[0]); q = int(INIT // (p0 * (1 + BUY_COST)))
    cash = INIT - q * p0 * (1 + BUY_COST)
    return cash + q * float(c.iloc[-1])


def main(telegram=False):
    wf = pickle.load(open(P('data_cache_wf.pkl'), 'rb')); big = pickle.load(open(P('data_cache_big.pkl'), 'rb'))
    dfs = {}
    for d in (big, wf):
        for mk in ('KOSPI', 'KOSDAQ'):
            for c, (n, df) in d.get(mk, {}).items():
                dfs.setdefault(c, (n, df))
    idx_df = wf['index']['KOSPI']
    L = ["💰 1000만원 한 종목 올인 — 봇 매매기법 vs 단순보유 (~10년, 1주단위·실비용)", ""]
    L.append(f"{'종목':12}{'봇매매':>12}{'단순보유':>12}{'승자':>7}")
    L.append("-" * 48)
    bw = hw = 0; bot_sum = hold_sum = 0
    for code in STOCKS:
        if code == '지수':
            nm, df = 'KOSPI지수', idx_df
        elif code in dfs:
            nm, df = dfs[code]
        else:
            continue
        if len(df[df.index <= END]) < 250:
            continue
        b = sim_bot(df); h = sim_hold(df)
        bot_sum += b; hold_sum += h
        win = '봇' if b > h else '보유'
        if b > h: bw += 1
        else: hw += 1
        L.append(f"{nm:12}{b/1e4:>10,.0f}만{h/1e4:>10,.0f}만{win:>7}")
    L.append("-" * 48)
    n = bw + hw
    L.append(f"{'평균':12}{bot_sum/n/1e4:>10,.0f}만{hold_sum/n/1e4:>10,.0f}만")
    L.append(f"\n봇 매매 승: {bw}/{n}종목 · 단순보유 승: {hw}/{n}종목")
    L.append(f"(시작 1000만 · 봇=바닥매수/천장매도 반복 · 단순보유=시작매수 후 보유)")
    L.append("⚠️ 15종목은 현재 상장 생존주(생존편향)라 절대액 과대. 봇vs보유 상대비교가 핵심.")
    rep = "\n".join(L)
    print(rep)
    if telegram:
        try:
            cc = sqlite3.connect(P('lassi.db'), timeout=30)
            r = cc.execute("SELECT telegram_token, telegram_chat_id FROM users WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone(); cc.close()
            from base.telegram_bot import TelegramNotifier
            TelegramNotifier(r[0], r[1]).send_message(rep); print("텔레그램 ✓")
        except Exception as e:
            print("텔레그램 실패", e)


if __name__ == '__main__':
    main('--telegram' in sys.argv)
