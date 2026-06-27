"""미국시장(미장) 프로그램 로직 8단계 백테스트 — KR 엔진/함수 재사용, 지수=S&P500(^GSPC).

국장(KR) 최근급등 편향 의심 → 효율적·장기 미장으로 교차검증.
같은 실제 함수(calculate_entry_score / bull·bear score / threshold) + 8단계 classify_phase 트리.
거래비용 미국 낮음(0.0005, 세금없음).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

import KR.regime_period_backtest as rpb
rpb.COST = 0.0005                      # 미국 거래비용(세금없음)
from KR.regime_period_backtest import _yf
from KR.program_logic_backtest import (_simulate, PHASE_ORDER, PHASE_KR, format_report, send_telegram)
from KR.phase_judge_pilot import classify8_series
from base.market_phase import _adx

US_SAMPLE = [
    ('AAPL','애플'),('MSFT','MS'),('GOOGL','구글'),('AMZN','아마존'),('NVDA','엔비디아'),
    ('META','메타'),('TSLA','테슬라'),('JPM','JP모건'),('JNJ','J&J'),('V','비자'),
    ('WMT','월마트'),('PG','P&G'),('XOM','엑슨'),('HD','홈디포'),('CVX','셰브론'),
    ('KO','코카콜라'),('PEP','펩시'),('MRK','머크'),('ABBV','애브비'),('COST','코스트코'),
    ('AVGO','브로드컴'),('ADBE','어도비'),('CRM','세일즈포스'),('NFLX','넷플릭스'),('AMD','AMD'),
    ('INTC','인텔'),('CSCO','시스코'),('QCOM','퀄컴'),('TXN','TI'),('ORCL','오라클'),
    ('DIS','디즈니'),('BA','보잉'),('NKE','나이키'),
]


def _ohlc_us(tkr):
    df = _yf(tkr)
    if df is None or len(df) < 300 or 'close' not in df.columns:
        return None
    return df[['open','high','low','close','volume']].astype(float).dropna(subset=['close'])


def backtest_stock(tkr, name, reg):
    df = _ohlc_us(tkr)
    if df is None:
        return None
    px = df['close']; reg2 = reg.reindex(px.index).ffill().fillna('SIDEWAYS')
    strats = {'단순보유':'hold','프로그램(공통점수)':'common',
              '프로그램(국면전용)':'phase','프로그램(국면전용+하락헤지)':'phase_hedge'}
    out = {}
    for nm, key in strats.items():
        eq = _simulate(df, reg2, key)
        dr = eq.pct_change().fillna(0.0)
        per = {}
        for ph in PHASE_ORDER:
            mask = (reg2 == ph).values
            per[ph] = round((np.prod(1.0+dr.values[mask])-1.0)*100,1) if mask.sum()>=15 else None
        per['전체'] = round((eq.iloc[-1]/eq.iloc[0]-1.0)*100,1)
        peak=eq.cummax(); per['MDD']=round(float(((eq/peak-1)*100).min()),1)
        out[nm]=per
    return out


def run(stocks=None):
    idx = _yf('^GSPC')
    phase = classify8_series(idx)
    feat = pd.DataFrame({'phase':phase,
                         'slope':(idx['close'].rolling(200).mean()/idx['close'].rolling(200).mean().shift(20)-1)*100,
                         'adx':_adx(idx['high'],idx['low'],idx['close']),
                         'mom20':(idx['close']/idx['close'].shift(20)-1)*100,
                         'mom60':(idx['close']/idx['close'].shift(60)-1)*100})
    stocks = stocks or US_SAMPLE
    agg={}; done=0
    for tkr,name in stocks:
        try: r=backtest_stock(tkr,name,phase)
        except Exception as e: print(f"  {name} 실패:{e}"); continue
        if not r: continue
        done+=1; print(f"  {name}({tkr}) 완료")
        for s,per in r.items():
            for k,v in per.items():
                if v is not None: agg.setdefault(s,{}).setdefault(k,[]).append(v)
    summary={s:{k:round(float(np.median(vs)),1) for k,vs in d.items()} for s,d in agg.items()}
    phase_feat={}
    for ph in PHASE_ORDER:
        sub=feat[feat['phase']==ph]
        if len(sub): phase_feat[ph]={'days':len(sub),'slope':round(sub['slope'].mean(),2),
                                     'adx':0,'mom20':round(sub['mom20'].mean(),1),'mom60':round(sub['mom60'].mean(),1)}
    return summary, done, phase_feat


if __name__=='__main__':
    n=int(sys.argv[1]) if len(sys.argv)>1 and sys.argv[1].isdigit() else len(US_SAMPLE)
    summary,done,pf=run(US_SAMPLE[:n])
    rep="🇺🇸 미장 "+format_report(summary,done,pf)
    print("\n"+rep)
    if '--tg' in sys.argv or n>=20:
        send_telegram(rep)
