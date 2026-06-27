"""기간-단위 국면 백테스트 — 상승/하락/횡보 기간별로 [단순보유 / 봇헤지 / 매매기법 / 조합] 수익률 비교.

BACKTEST_TODO 7단계 구현 (신호-DB가 아니라 '연속가격 + 일별 국면' 기반 → 봇 헤지(#7) 비교 가능):
1. 봇 내장로직 vs 여러 매매법 단순 수익률 비교 (상승/하락/횡보)
6. 국면 파악 후 그 기간만 백테스트
7. 하락장 = 봇 헤지(국면 비-상승 → 현금) 도 같이 비교 ★ (101490서 봇 8단계 헤지만 +18%)

연속가격은 30-50 표본종목만 1회 fetch(pykrx) — '전종목 재수집 금지'는 8M 얘기, 표본은 OK.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

COST = 0.0021   # 편도 거래비용+세금 근사 (KR)

# 다양한 표본 (대형/중형/소형 + 섹터 분산) — 확장 가능
SAMPLE_KR = [
    ('005930', '삼성전자'), ('000660', 'SK하이닉스'), ('035420', 'NAVER'),
    ('051910', 'LG화학'), ('005380', '현대차'), ('068270', '셀트리온'),
    ('035720', '카카오'), ('105560', 'KB금융'), ('012330', '현대모비스'),
    ('101490', '에스앤에스텍'), ('000250', '삼천당제약'), ('247540', '에코프로비엠'),
    ('086520', '에코프로'), ('028300', 'HLB'), ('196170', '알테오젠'),
]


def _yf(sym):
    import yfinance as yf
    df = yf.download(sym, start='2014-01-01', end='2026-12-31', progress=False, auto_adjust=True)
    if df is None or df.empty:
        return None
    if hasattr(df.columns, 'get_level_values'):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]
    return df


def _ohlc(code):
    """yfinance 조정종가(수익률 정확). KOSPI .KS 우선, 비면 KOSDAQ .KQ."""
    for suf in ('.KS', '.KQ'):
        df = _yf(code + suf)
        if df is not None and len(df) > 250 and 'close' in df.columns:
            return df[['open', 'high', 'low', 'close', 'volume']].astype(float).dropna(subset=['close'])
    return None


def _kospi_phase_daily():
    """KOSPI(^KS11) 일별 3대 국면(상승/하락/횡보) — 200MA·모멘텀 기반(walk-forward)."""
    idx = _yf('^KS11')
    if idx is None:
        return pd.Series(dtype=object)
    c = idx['close'].astype(float)
    ma200 = c.rolling(200).mean(); ma60 = c.rolling(60).mean()
    mom60 = c.pct_change(60)
    reg = pd.Series('횡보', index=c.index)
    reg[(c > ma200) & (mom60 > 0.03)] = '상승'
    reg[(c < ma200) & (mom60 < -0.03)] = '하락'
    return reg


# ── 기법(매매신호) — 연속가격에서 보유여부(bool series) 생성 ──
def sig_ma_cross(df):
    m5, m20 = df['close'].rolling(5).mean(), df['close'].rolling(20).mean()
    return m5 > m20

def sig_golden(df):
    m50, m200 = df['close'].rolling(50).mean(), df['close'].rolling(200).mean()
    return m50 > m200

def sig_rsi(df, lo=35):
    d = df['close'].diff(); up = d.clip(lower=0).rolling(14).mean(); dn = (-d.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + up / (dn + 1e-9))
    return rsi < 70   # 과매도 진입~과열 이탈(보유구간)

def sig_macd(df):
    e12 = df['close'].ewm(span=12).mean(); e26 = df['close'].ewm(span=26).mean()
    macd = e12 - e26; sigl = macd.ewm(span=9).mean()
    return macd > sigl

def sig_above200(df):
    return df['close'] > df['close'].rolling(200).mean()

METHODS = {'MA교차': sig_ma_cross, '골든크로스': sig_golden, 'RSI': sig_rsi,
           'MACD': sig_macd, '200일선': sig_above200}


def _run(px, hold_bool):
    """hold_bool=True인 날만 보유. 진입/청산시 비용. 누적수익% 반환."""
    cash, sh, eq = 1.0, 0.0, []
    prev = False
    for d in px.index:
        p = px.loc[d]; h = bool(hold_bool.loc[d]) if d in hold_bool.index else False
        if h and not prev:
            sh = cash * (1 - COST) / p; cash = 0.0
        elif not h and prev:
            cash = sh * p * (1 - COST); sh = 0.0
        prev = h; eq.append(cash + sh * p)
    e = pd.Series(eq, index=px.index)
    return e


def backtest_stock(code, name, reg):
    df = _ohlc(code)
    if df is None or len(df) < 250:
        return None
    px = df['close']; reg2 = reg.reindex(px.index).ffill()
    bull_now = (reg2 == '상승')
    strategies = {'단순보유': pd.Series(True, index=px.index),
                  '봇헤지(상승만보유)': bull_now,        # 풀헤지: 비-상승이면 현금
                  '적응형(하락만현금)': (reg2 != '하락')}  # 국면조건부: 하락장에만 헤지, 상승·횡보 보유
    for nm, fn in METHODS.items():
        try: strategies[nm] = fn(df).reindex(px.index).fillna(False)
        except Exception: pass
    # 2-3 조합(AND): MA+RSI, MACD+200일선, MA+MACD+RSI
    try: strategies['조합:MA+RSI'] = (sig_ma_cross(df) & sig_rsi(df)).reindex(px.index).fillna(False)
    except Exception: pass
    try: strategies['조합:MACD+200일선'] = (sig_macd(df) & sig_above200(df)).reindex(px.index).fillna(False)
    except Exception: pass
    try: strategies['조합:MA+MACD+RSI'] = (sig_ma_cross(df) & sig_macd(df) & sig_rsi(df)).reindex(px.index).fillna(False)
    except Exception: pass

    out = {}
    for nm, hb in strategies.items():
        eq = _run(px, hb)
        dr = eq.pct_change().fillna(0.0)        # 전략 일별수익
        per = {}
        for rg in ('상승', '하락', '횡보'):
            mask = (reg2 == rg).values
            if mask.sum() < 20:
                per[rg] = None; continue
            # 그 국면 날들의 일별수익만 복리 (비연속 구간 정확 격리)
            per[rg] = round((np.prod(1.0 + dr.values[mask]) - 1.0) * 100, 1)
        per['전체'] = round((eq.iloc[-1] / eq.iloc[0] - 1.0) * 100, 1)
        # 위험: 최대낙폭(MDD) + 위험조정(전체/|MDD|)
        peak = eq.cummax(); dd = (eq / peak - 1.0) * 100
        per['MDD'] = round(float(dd.min()), 1)
        per['수익MDD'] = round(per['전체'] / (abs(per['MDD']) + 1), 2)
        out[nm] = per
    return out


def run_sample(stocks=None, verbose=True):
    reg = _kospi_phase_daily()
    stocks = stocks or SAMPLE_KR
    agg = {}   # strategy -> regime -> [returns]
    for code, name in stocks:
        r = backtest_stock(code, name, reg)
        if not r:
            continue
        if verbose:
            print(f"  {name}({code}) 완료")
        for strat, per in r.items():
            for rg, v in per.items():
                if v is not None:
                    agg.setdefault(strat, {}).setdefault(rg, []).append(v)
    # 집계: 국면별 중앙값 수익률
    summary = {}
    for strat, perreg in agg.items():
        summary[strat] = {rg: round(float(np.median(vs)), 1) for rg, vs in perreg.items()}
    return summary


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    s = run_sample(SAMPLE_KR[:n])
    print("\n=== 국면별 수익률 + 위험(MDD) + 위험조정 중앙값 (표본 %d종목) ===" % n)
    print(f"{'전략':22} {'상승':>7} {'하락':>7} {'횡보':>7} {'전체':>7} {'MDD':>7} {'수익/MDD':>8}")
    order = sorted(s.items(), key=lambda kv: kv[1].get('수익MDD', -999), reverse=True)
    for strat, per in order:
        g = lambda k: str(per.get(k, '-'))
        print(f"{strat:22} {g('상승'):>7} {g('하락'):>7} {g('횡보'):>7} {g('전체'):>7} {g('MDD'):>7} {g('수익MDD'):>8}")
