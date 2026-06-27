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
    # 대형(다양 섹터)
    ('005930', '삼성전자'), ('000660', 'SK하이닉스'), ('035420', 'NAVER'),
    ('051910', 'LG화학'), ('005380', '현대차'), ('068270', '셀트리온'),
    ('035720', '카카오'), ('105560', 'KB금융'), ('012330', '현대모비스'),
    ('005490', 'POSCO홀딩스'), ('015760', '한국전력'), ('055550', '신한지주'),
    ('017670', 'SK텔레콤'), ('066570', 'LG전자'), ('000270', '기아'),
    ('006400', '삼성SDI'), ('086790', '하나금융'), ('207940', '삼성바이오'),
    ('033780', 'KT&G'), ('011170', '롯데케미칼'), ('010130', '고려아연'),
    ('009150', '삼성전기'), ('316140', '우리금융'), ('259960', '크래프톤'),
    ('097950', 'CJ제일제당'), ('271560', '오리온'), ('004990', '롯데지주'),
    # 중소/성장(변동성)
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

def _rsi(df, n=14):
    d = df['close'].diff(); up = d.clip(lower=0).rolling(n).mean(); dn = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + up / (dn + 1e-9))

def _swing(entry, exit_):
    """평균회귀 스윙: entry True에 진입, exit True까지 보유(상태기반)."""
    e = entry.fillna(False).values; x = exit_.fillna(False).values
    hold = np.zeros(len(e), dtype=bool); h = False
    for i in range(len(e)):
        if not h and e[i]: h = True
        elif h and x[i]: h = False
        hold[i] = h
    return pd.Series(hold, index=entry.index)

# 진짜 스윙(평균회귀) — 횡보장용: 과매도 매수→과매수 매도
def sig_swing_rsi(df):
    r = _rsi(df); return _swing(r < 35, r > 65)
def sig_swing_bb(df):
    ma = df['close'].rolling(20).mean(); sd = df['close'].rolling(20).std()
    return _swing(df['close'] < ma - 2 * sd, df['close'] > ma + 2 * sd)
def sig_swing_stoch(df):
    lo = df['low'].rolling(14).min(); hi = df['high'].rolling(14).max()
    k = (df['close'] - lo) / (hi - lo + 1e-9) * 100
    return _swing(k < 20, k > 80)

METHODS = {'MA교차': sig_ma_cross, '골든크로스': sig_golden, 'RSI(추세)': sig_rsi,
           'MACD': sig_macd, '200일선': sig_above200,
           '스윙RSI(반전)': sig_swing_rsi, '스윙볼린저': sig_swing_bb, '스윙스토캐스틱': sig_swing_stoch}


def _run(px, hold_bool):
    """hold_bool=True인 날만 보유(전량 in/out). 누적수익% 반환."""
    return _run_frac(px, hold_bool.astype(float))


def _run_frac(px, weight):
    """목표 보유비중(0~1) 시리즈대로 리밸런싱. 비중 변할 때 거래분에 비용. 실제 봇 부분헤지용."""
    w = weight.reindex(px.index).ffill().fillna(0.0).clip(0, 1)
    cash, sh, eq = 1.0, 0.0, []
    prevw = 0.0
    for d in px.index:
        p = float(px.loc[d]); tw = float(w.loc[d])
        nav = cash + sh * p
        target_val = tw * nav
        cur_val = sh * p
        if abs(target_val - cur_val) > nav * 0.01:        # 1%+ 차이만 리밸런싱
            delta = target_val - cur_val
            cost = abs(delta) * COST
            sh = target_val / p if p > 0 else 0.0
            cash = nav - target_val - cost
        eq.append(cash + sh * p)
        prevw = tw
    return pd.Series(eq, index=px.index)


def backtest_stock(code, name, reg):
    df = _ohlc(code)
    if df is None or len(df) < 250:
        return None
    px = df['close']; reg2 = reg.reindex(px.index).ffill()
    bull_now = (reg2 == '상승')
    bear_now = (reg2 == '하락')
    # ── 실제 프로그램 봇 로직 부분헤지 (KR/bot.py 기준) ──
    # 코어: 50% floor 절대미매도 → 코어포지션은 항상 100% 보유(=보유와 동일)
    # 위성: BEAR시 30% 트림 → BEAR 70%, 그 외 100%
    w_core = pd.Series(1.0, index=px.index)                       # 코어(floor) = 사실상 보유
    w_sat = pd.Series(1.0, index=px.index); w_sat[bear_now] = 0.70  # 위성 BEAR 30%트림
    # 포트폴리오 봇(코어50%+위성50% 가정): BEAR시 0.5*1.0 + 0.5*0.7 = 0.85
    w_bot = 0.5 * w_core + 0.5 * w_sat
    bin_strats = {'단순보유': pd.Series(True, index=px.index),
                  '봇 풀헤지(대용-전량현금)': bull_now,       # 옛 대용품(참고)
                  '적응형(하락만현금)': (reg2 != '하락')}
    frac_strats = {'봇 실제(코어50%+위성트림)': w_bot,
                   '봇 위성(BEAR 30%트림)': w_sat}
    for nm, fn in METHODS.items():
        try: bin_strats[nm] = fn(df).reindex(px.index).fillna(False)
        except Exception: pass
    try: bin_strats['조합:MA+RSI'] = (sig_ma_cross(df) & sig_rsi(df)).reindex(px.index).fillna(False)
    except Exception: pass
    try: bin_strats['조합:MACD+200일선'] = (sig_macd(df) & sig_above200(df)).reindex(px.index).fillna(False)
    except Exception: pass
    try: bin_strats['조합:MA+MACD+RSI'] = (sig_ma_cross(df) & sig_macd(df) & sig_rsi(df)).reindex(px.index).fillna(False)
    except Exception: pass
    # ── 복합기법(투표/가중) ──
    try:
        votes = (sig_ma_cross(df).astype(int) + sig_rsi(df).astype(int) + sig_macd(df).astype(int))
        bin_strats['복합:투표2of3(MA/RSI/MACD)'] = (votes >= 2).reindex(px.index).fillna(False)
        bin_strats['복합:OR(MA|MACD|돌파)'] = (sig_ma_cross(df) | sig_macd(df) | (df['close'] > df['close'].rolling(20).max().shift(1))).reindex(px.index).fillna(False)
    except Exception: pass
    # ── 복합기법 + 봇 내장로직(헤지) 결합 = 신호진입 후 BEAR엔 봇처럼 부분트림 ──
    try:
        vote2 = (votes >= 2).reindex(px.index).fillna(False).astype(float)
        wv = vote2.copy(); wv[bear_now & (vote2 > 0)] = 0.7   # 복합 보유 중 BEAR면 30%트림(봇식)
        frac_strats['복합투표+봇헤지'] = wv
        gc = sig_golden(df).reindex(px.index).fillna(False).astype(float)
        wg = gc.copy(); wg[bear_now & (gc > 0)] = 0.7
        frac_strats['골든크로스+봇헤지'] = wg
    except Exception: pass

    # 전부 비중(0~1)으로 통일해 _run_frac 실행
    weights = {nm: hb.astype(float) for nm, hb in bin_strats.items()}
    weights.update(frac_strats)
    out = {}
    for nm, w in weights.items():
        eq = _run_frac(px, w)
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
    print("\n############ 국면별 전략 순위 (표본 %d종목, 수익률 중앙값) ############" % n)
    for rg in ('상승', '하락', '횡보'):
        print(f"\n===== [{rg}장] — 이 국면 수익률 높은 순 =====")
        order = sorted(s.items(), key=lambda kv: kv[1].get(rg, -9999), reverse=True)
        for i, (strat, per) in enumerate(order, 1):
            v = per.get(rg, '-')
            print(f"  {i:2}. {strat:24} {str(v):>7}%")
