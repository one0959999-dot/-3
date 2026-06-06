"""
[대형주 8종목] 종합 전략 백테스팅
- 종목: 삼성전자, SK하이닉스, NAVER, 현대차, 카카오, POSCO홀딩스, 셀트리온, LG에너지솔루션
- 기간: 최근 1년 (약 250 거래일)
- 초기 자금: 종목당 1,000만원
- 결과: 전략별 8종목 평균 수익률 랭킹
"""

from pykrx import stock
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

TICKERS = {
    "003850": "보령",
}
INITIAL_CASH = 10_000_000


# ─── 데이터 로드 ───
def fetch_data(ticker):
    end_date   = datetime.today()
    start_date = end_date - timedelta(days=400)  # 지표 계산용 여유 포함
    df = stock.get_market_ohlcv_by_date(
        start_date.strftime("%Y%m%d"),
        end_date.strftime("%Y%m%d"),
        ticker
    )
    df.rename(columns={'시가':'open','고가':'high','저가':'low','종가':'close','거래량':'volume'}, inplace=True)
    return df.dropna(subset=['close'])

# ─── 시뮬레이션 ───
def simulate(df, signals):
    sim_df = df.tail(250)  # 마지막 250 거래일(약 1년)
    signals = signals.reindex(sim_df.index, fill_value=0)
    cash, holding, buy_price = INITIAL_CASH, 0, 0
    for date in sim_df.index:
        price = int(sim_df.loc[date, 'close'])
        sig   = signals.loc[date]
        if sig == 1 and holding == 0 and cash >= price:
            holding   = cash // price
            cash     -= holding * price
            buy_price = price
        elif sig == -1 and holding > 0:
            cash    += holding * price
            holding  = 0
    if holding > 0:
        cash += holding * int(sim_df.iloc[-1]['close'])
    return (cash - INITIAL_CASH) / INITIAL_CASH * 100

# ─── 지표 계산 ───
def ema(s, span):    return s.ewm(span=span, adjust=False).mean()

def rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / (l + 1e-10))

def macd(s, f=12, sl=26, sig=9):
    m = ema(s,f) - ema(s,sl); return m, ema(m,sig)

def bollinger(s, p=20, k=2):
    mid = s.rolling(p).mean(); sd = s.rolling(p).std()
    return mid+k*sd, mid, mid-k*sd

def stochastic(h, l, c, kp=14, dp=3):
    lo = l.rolling(kp).min(); hi = h.rolling(kp).max()
    k  = 100*(c-lo)/(hi-lo+1e-10); return k, k.rolling(dp).mean()

def cci_ind(h,l,c,p=20):
    tp = (h+l+c)/3; ma = tp.rolling(p).mean()
    md = tp.rolling(p).apply(lambda x: np.mean(np.abs(x-x.mean())), raw=True)
    return (tp-ma)/(0.015*md+1e-10)

def williams_r(h,l,c,p=14):
    return -100*(h.rolling(p).max()-c)/(h.rolling(p).max()-l.rolling(p).min()+1e-10)

def parabolic_sar(high, low, af_step=0.02, af_max=0.2):
    sar, ep, af, bull = low.iloc[0], high.iloc[0], af_step, True
    result = []
    for i in range(len(high)):
        result.append(sar)
        if bull:
            if low.iloc[i] < sar: bull, sar, ep, af = False, ep, low.iloc[i], af_step
            else:
                if high.iloc[i] > ep: ep = high.iloc[i]; af = min(af+af_step, af_max)
                sar = sar + af*(ep-sar)
        else:
            if high.iloc[i] > sar: bull, sar, ep, af = True, ep, high.iloc[i], af_step
            else:
                if low.iloc[i] < ep: ep = low.iloc[i]; af = min(af+af_step, af_max)
                sar = sar + af*(ep-sar)
    return pd.Series(result, index=high.index)

def cross_signal(fast, slow):
    s = pd.Series(0, index=fast.index)
    s[fast>slow]=1; s[fast<slow]=-1
    t = s.diff().fillna(0)
    out = pd.Series(0, index=s.index)
    out[t>0]=1; out[t<0]=-1
    return out

# ─── 전략 모음 ───
def get_strategies(df):
    h,l,c,v = df['high'],df['low'],df['close'],df['volume']
    strats = {}

    # 이동평균선 (SMA)
    for f,s in [(5,20),(5,60),(3,10),(10,30),(3,20)]:
        strats[f'SMA {f}/{s} 크로스'] = cross_signal(c.rolling(f).mean(), c.rolling(s).mean())

    # EMA 크로스
    for f,s in [(5,20),(3,10),(12,26)]:
        strats[f'EMA {f}/{s} 크로스'] = cross_signal(ema(c,f), ema(c,s))

    # RSI
    for p,lo,hi in [(14,30,70),(9,30,70),(14,40,60)]:
        r = rsi(c,p); sig = pd.Series(0,index=c.index)
        sig[r<lo]=1; sig[r>hi]=-1
        strats[f'RSI({p}) {lo}/{hi}'] = sig[sig!=0].reindex(c.index,fill_value=0)

    # MACD
    m,ms = macd(c)
    strats['MACD 시그널 크로스'] = cross_signal(m,ms)

    # 볼린저밴드
    bu,bm,bl = bollinger(c)
    for name,(buy_cond,sell_cond) in {
        '볼린저밴드 반전': (c<bl, c>bu),
        '볼린저밴드 추세': (c>bu, c<bl),
        '볼린저밴드 중심선': (cross_signal(c,bm)==1, cross_signal(c,bm)==-1),
    }.items():
        sig = pd.Series(0,index=c.index)
        if name=='볼린저밴드 중심선':
            strats[name] = buy_cond.map({True:1,False:0}) + sell_cond.map({True:-1,False:0})
        else:
            sig[buy_cond]=1; sig[sell_cond]=-1
            strats[name] = sig[sig!=0].reindex(c.index,fill_value=0)

    # Stochastic
    k,d = stochastic(h,l,c)
    strats['Stochastic K/D 크로스'] = cross_signal(k,d)

    # CCI
    cc = cci_ind(h,l,c); sig=pd.Series(0,index=c.index)
    sig[cc<-100]=1; sig[cc>100]=-1
    strats['CCI(20) ±100'] = sig[sig!=0].reindex(c.index,fill_value=0)

    # Williams %R
    wr = williams_r(h,l,c); sig=pd.Series(0,index=c.index)
    sig[wr<-80]=1; sig[wr>-20]=-1
    strats['Williams %R'] = sig[sig!=0].reindex(c.index,fill_value=0)

    # ROC
    roc=c.pct_change(5)*100; sig=pd.Series(0,index=c.index)
    sig[roc<-3]=1; sig[roc>3]=-1
    strats['ROC(5일) 반전'] = sig[sig!=0].reindex(c.index,fill_value=0)

    # Parabolic SAR
    psar=parabolic_sar(h,l)
    strats['Parabolic SAR'] = cross_signal(c,psar)

    # VWMA
    vs=(c*v).rolling(5).sum()/v.rolling(5).sum()
    vl=(c*v).rolling(20).sum()/v.rolling(20).sum()
    strats['VWMA(5/20) 크로스'] = cross_signal(vs,vl)

    # 복합 전략
    r14=rsi(c,14); e5=ema(c,5); e20=ema(c,20)
    sig=pd.Series(0,index=c.index)
    sig[(r14<40)&(e5>e20)]=1; sig[(r14>60)&(e5<e20)]=-1
    strats['복합: RSI+EMA'] = sig[sig!=0].reindex(c.index,fill_value=0)

    m2,ms2=macd(c); sig=pd.Series(0,index=c.index)
    sig[(m2>ms2)&(c<bm)]=1; sig[(m2<ms2)&(c>bm)]=-1
    strats['복합: MACD+볼린저'] = sig[sig!=0].reindex(c.index,fill_value=0)

    k2,d2=stochastic(h,l,c); r2=rsi(c,14); sig=pd.Series(0,index=c.index)
    sig[(k2<20)&(r2<35)]=1; sig[(k2>80)&(r2>65)]=-1
    strats['복합: Stoch+RSI'] = sig[sig!=0].reindex(c.index,fill_value=0)

    # Buy & Hold
    sim_c=c.tail(250)
    bh=(sim_c.iloc[-1]-sim_c.iloc[0])/sim_c.iloc[0]*100
    strats['📌 Buy & Hold (기준)'] = bh  # 숫자로 직접 저장

    return strats

# ─── 메인 ───
if __name__ == '__main__':
    all_results = {}  # {전략명: [종목별 수익률]}

    print(f"\n{'='*62}")
    print(f"  [대형주 8종목] 전략별 수익률 백테스팅 (최근 1년)")
    print(f"{'='*62}")

    for ticker, name in TICKERS.items():
        print(f"  📡 [{name}] 데이터 로드 중...", end=" ", flush=True)
        try:
            df = fetch_data(ticker)
            strats = get_strategies(df)
            for sname, sig_or_val in strats.items():
                if sname not in all_results:
                    all_results[sname] = []
                if isinstance(sig_or_val, (int, float)):
                    all_results[sname].append(sig_or_val)
                else:
                    all_results[sname].append(simulate(df, sig_or_val))
            print(f"완료! (1년 수익 Buy&Hold: {all_results['📌 Buy & Hold (기준)'][-1]:+.1f}%)")
        except Exception as e:
            print(f"오류: {e}")

    # 평균 수익률 계산
    avg_results = {k: np.mean(v) for k, v in all_results.items() if len(v) > 0}
    sorted_results = sorted(avg_results.items(), key=lambda x: x[1], reverse=True)

    print(f"\n{'='*65}")
    print(f"  📊 전략별 8종목 평균 수익률 랭킹 (1년)")
    print(f"  {'순위':<4} {'전략명':<24} {'평균 수익률':>11}   {'평균 최종 자산':>15}")
    print(f"  {'-'*62}")

    medals = ['🥇','🥈','🥉']
    for i,(sname,ret) in enumerate(sorted_results):
        rank  = medals[i] if i<3 else f"  {i+1:2d}."
        final = INITIAL_CASH*(1+ret/100)
        sign  = '+' if ret>=0 else ''
        mark  = ' ◀ 최고!' if i==0 else (' ◀ BuyHold' if '📌' in sname else '')
        print(f"  {rank:<4} {sname:<24} {sign}{ret:>9.2f}%   {final:>13,.0f}원{mark}")

    print(f"{'='*65}")
    best_name, best_ret = sorted_results[0]
    bh_ret = avg_results.get('📌 Buy & Hold (기준)', 0)
    beat = best_ret - bh_ret
    print(f"\n  🏆 최고 전략: [{best_name}]  평균 {best_ret:+.2f}%")
    print(f"  📌 Buy & Hold 평균: {bh_ret:+.2f}%")
    if beat > 0:
        print(f"  ✅ 최고 전략이 단순 보유 대비 {beat:+.2f}%p 더 좋았습니다!\n")
    else:
        print(f"  ⚠️  1년 기간에도 단순 보유가 가장 나은 결과였습니다. ({beat:.2f}%p 차이)\n")
