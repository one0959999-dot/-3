"""
코어-위성(Core-Satellite) 전략 백테스팅
- 코어: 보령(003850) 30% 초기 매수 후 절대 매도 안함
- 위성: SK하이닉스, 현대차, POSCO홀딩스, NAVER (각 위성 자금 25%씩)
- 위성 전략: RSI(9) 30/70 (보령 백테스트에서 최고 성적)
- 재투자: 위성 수익 실현 시 수익금의 50%를 보령 추가 매수
- 기간: 최근 1년 (약 250 거래일)
- 초기 자금: 1,000만원
"""

from pykrx import stock
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

# ─── 설정 ───
INITIAL_CAPITAL   = 10_000_000
CORE_RATIO        = 0.30   # 보령 초기 투자 비율
SATELLITE_RATIO   = 0.70   # 위성 트레이딩 비율
REINVEST_RATIO    = 0.50   # 위성 수익 중 보령 재투자 비율

CORE_TICKER   = "003850"
CORE_NAME     = "보령"

SATELLITE_TICKERS = {
    "000660": "SK하이닉스",
    "005380": "현대차",
    "005490": "POSCO홀딩스",
    "035420": "NAVER",
}

# ─── 데이터 로드 ───
def fetch(ticker):
    end   = datetime.today()
    start = end - timedelta(days=420)
    df = stock.get_market_ohlcv_by_date(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker)
    df.rename(columns={'시가':'open','고가':'high','저가':'low','종가':'close','거래량':'volume'}, inplace=True)
    return df.dropna(subset=['close']).tail(300)

# ─── RSI 계산 ───
def calc_rsi(series, period=9):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    return 100 - 100 / (1 + gain / (loss + 1e-10))

# ─── RSI(9) 매매 신호 생성 ───
def rsi_signals(df):
    r = calc_rsi(df['close'], 9)
    sig = pd.Series(0, index=df.index)
    sig[r < 30] =  1   # 과매도 → 매수
    sig[r > 70] = -1   # 과매수 → 매도
    # 연속 신호 중 첫 번째만 유효하게
    prev = 0
    for i in sig.index:
        if sig[i] == prev:
            sig[i] = 0
        elif sig[i] != 0:
            prev = sig[i]
    return sig

# ─── 시뮬레이션 ───
def run():
    print(f"\n{'='*60}")
    print(f"  코어-위성 전략 백테스팅 (최근 1년)")
    print(f"  초기 자금: {INITIAL_CAPITAL:,}원")
    print(f"  코어(보령) {int(CORE_RATIO*100)}%  /  위성 {int(SATELLITE_RATIO*100)}%")
    print(f"  위성 수익의 {int(REINVEST_RATIO*100)}%를 보령 재투자")
    print(f"{'='*60}\n")

    # ── 데이터 로드 ──
    print("📡 데이터 로드 중...")
    core_df = fetch(CORE_TICKER)
    sat_dfs  = {}
    for ticker, name in SATELLITE_TICKERS.items():
        sat_dfs[ticker] = fetch(ticker)
        print(f"   [{name}] 완료")

    # ── 초기 자산 배분 ──
    core_budget = INITIAL_CAPITAL * CORE_RATIO
    sat_budget  = INITIAL_CAPITAL * SATELLITE_RATIO
    per_sat     = sat_budget / len(SATELLITE_TICKERS)

    # 보령 초기 매수
    sim_core = core_df.tail(250)
    core_init_price  = int(sim_core.iloc[0]['close'])
    core_shares      = int(core_budget // core_init_price)
    core_cash_remain = core_budget - core_shares * core_init_price  # 잔돈
    reinvest_log     = []

    print(f"\n💎 [코어] 보령 초기 매수: {core_init_price:,}원 × {core_shares}주 = {core_shares*core_init_price:,}원")

    # ── 위성 트레이딩 ──
    total_sat_profit = 0
    total_sat_final  = 0
    sat_results = []

    for ticker, name in SATELLITE_TICKERS.items():
        df  = sat_dfs[ticker].tail(250)
        sig = rsi_signals(df)
        cash, holding, buy_price = per_sat, 0, 0
        trades = []

        for date in df.index:
            price = int(df.loc[date, 'close'])
            s     = sig.loc[date]

            if s == 1 and holding == 0 and cash >= price:
                holding   = cash // price
                cash     -= holding * price
                buy_price = price

            elif s == -1 and holding > 0:
                revenue = holding * price
                profit  = revenue - (buy_price * holding)
                cash   += revenue

                if profit > 0:
                    reinvest_amt = profit * REINVEST_RATIO
                    # 보령 추가 매수
                    boryung_price = int(sim_core.loc[date, 'close']) if date in sim_core.index else int(sim_core.iloc[-1]['close'])
                    new_shares    = int(reinvest_amt // boryung_price)
                    if new_shares > 0:
                        core_shares += new_shares
                        cash        -= (reinvest_amt - new_shares * boryung_price)  # 잔돈 환원
                        reinvest_log.append({
                            'date':   date.strftime('%Y-%m-%d'),
                            'from':   name,
                            'profit': profit,
                            'reinvest': reinvest_amt,
                            'bought': new_shares,
                            'bprice': boryung_price,
                            'total_boryung': core_shares
                        })

                trades.append({'date': date, 'profit': profit})
                holding = 0

        # 미체결 청산
        if holding > 0:
            final_price = int(df.iloc[-1]['close'])
            cash += holding * final_price

        sat_profit = cash - per_sat
        total_sat_profit += sat_profit
        total_sat_final  += cash
        sat_results.append((name, per_sat, cash, sat_profit, len(trades)))

    # ── 최종 보령 가치 계산 ──
    core_final_price = int(sim_core.iloc[-1]['close'])
    core_value       = core_shares * core_final_price
    total_value      = core_value + total_sat_final + core_cash_remain

    # ── 결과 출력 ──
    print(f"\n{'─'*60}")
    print(f"  📈 위성 트레이딩 결과")
    print(f"{'─'*60}")
    for name, budget, final, profit, ntrades in sat_results:
        sign = '+' if profit >= 0 else ''
        print(f"  [{name:<12}] 투자: {budget:>10,.0f}원  →  결과: {final:>10,.0f}원  ({sign}{profit/budget*100:.1f}%)  거래{ntrades}회")

    print(f"\n{'─'*60}")
    print(f"  💎 보령 코어 성장 내역")
    print(f"{'─'*60}")
    print(f"  초기 보유량:   {int(INITIAL_CAPITAL*CORE_RATIO//core_init_price):>6}주 @ {core_init_price:,}원")
    if reinvest_log:
        print(f"\n  재투자 이벤트:")
        for r in reinvest_log:
            print(f"    {r['date']}  [{r['from']}] 수익 {r['profit']:+,.0f}원 → 보령 {r['bought']}주 추가 매수 (누적: {r['total_boryung']}주)")
    else:
        print("  (위성에서 수익 실현 없어 재투자 이벤트 없음)")

    init_shares = int(INITIAL_CAPITAL * CORE_RATIO // core_init_price)
    added_shares = core_shares - init_shares
    print(f"\n  📊 보령 주식 성장:")
    print(f"     최초 보유:  {init_shares:>5}주")
    print(f"     추가 매수:  {added_shares:>5}주  (위성 수익 재투자)")
    print(f"     최종 보유:  {core_shares:>5}주  (+{added_shares/init_shares*100:.1f}%)")
    print(f"     평가 금액:  {core_value:>12,.0f}원  (현재가 {core_final_price:,}원)")

    print(f"\n{'='*60}")
    print(f"  🏆 코어-위성 전략 최종 결과")
    print(f"{'='*60}")
    print(f"  초기 투자금:   {INITIAL_CAPITAL:>12,}원")
    print(f"  위성 총 자산:  {total_sat_final:>12,.0f}원")
    print(f"  보령 평가액:   {core_value:>12,.0f}원  ({core_shares}주)")
    print(f"  기타 잔돈:     {core_cash_remain:>12,.0f}원")
    print(f"  ─────────────────────────────────────")
    print(f"  최종 총 자산:  {total_value:>12,.0f}원")
    total_ret = (total_value - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    print(f"  총 수익률:     {total_ret:>+11.2f}%")

    # 비교: 그냥 보령만 샀을 때
    bh_shares = INITIAL_CAPITAL // core_init_price
    bh_value  = bh_shares * core_final_price
    bh_ret    = (bh_value - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    print(f"\n  📌 비교: 1,000만원 전부 보령만 샀을 때")
    print(f"           → {bh_shares}주 × {core_final_price:,}원 = {bh_value:,}원 ({bh_ret:+.2f}%)")
    print(f"  📌 비교: 1,000만원 전부 현금 보유했을 때 → 0.00%")
    print(f"{'='*60}\n")

if __name__ == '__main__':
    run()
