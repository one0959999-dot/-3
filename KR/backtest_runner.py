"""
야간 백테스트 러너
- 장 마감 후 (15:40~) 자동 실행
- KR 전체 종목을 순환하며 AI 백테스트
- 하루 100종목씩 → 25일에 전체 1바퀴
- AI 판단 + 실제 결과를 ai_decision_log에 저장 (session_type='backtest')
"""
import time
import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger('lassi_bot')

# 하루에 처리할 종목 수
DAILY_BATCH_SIZE = 100
# 종목당 백테스트할 최근 기간 (거래일 기준)
BACKTEST_DAYS = 250  # 약 1년


def _get_all_kr_tickers() -> list[dict]:
    """KR 전체 종목 리스트 반환 (yfinance 기반)."""
    try:
        import yfinance as yf
        # pykrx로 전체 종목 가져오기
        try:
            from pykrx import stock as pykrx_stock
            today = datetime.now().strftime('%Y%m%d')
            kospi = pykrx_stock.get_market_ticker_list(today, market='KOSPI')
            kosdaq = pykrx_stock.get_market_ticker_list(today, market='KOSDAQ')
            tickers = []
            for t in kospi + kosdaq:
                name = pykrx_stock.get_market_ticker_name(t)
                tickers.append({'ticker': t, 'name': name, 'market': 'KOSPI' if t in kospi else 'KOSDAQ'})
            return tickers
        except ImportError:
            pass

        # pykrx 없으면 Toss API 스크리닝 결과 활용
        from KR.screener import get_all_tickers_fallback
        return get_all_tickers_fallback()
    except Exception as e:
        logger.warning(f"[백테스트] 종목 리스트 조회 실패: {e}")
        return []


def _get_historical_ohlcv(ticker: str, days: int = 300) -> Optional[pd.DataFrame]:
    """yfinance로 과거 OHLCV 데이터 조회."""
    try:
        import yfinance as yf
        yfk = ticker + '.KS'
        df = yf.download(yfk, period=f'{days}d', interval='1d',
                         progress=False, auto_adjust=True)
        if df.empty:
            yfk = ticker + '.KQ'
            df = yf.download(yfk, period=f'{days}d', interval='1d',
                             progress=False, auto_adjust=True)
        if df.empty:
            return None
        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index)
        return df
    except Exception as e:
        logger.debug(f"[백테스트] {ticker} OHLCV 조회 실패: {e}")
        return None


def run_backtest_for_ticker(ticker: str, stock_name: str, user_id: int,
                             claude_client, toss_api=None) -> int:
    """
    단일 종목 백테스트 실행.
    과거 데이터를 날짜순으로 재생하며 AI 판단 + 실제 결과 기록.
    반환: 완료된 시나리오 수
    """
    from base.database import log_ai_decision, update_ai_decision_outcome, get_recent_trades, load_ai_rules
    from KR.strategy import calc_rsi, get_market_regime

    df = _get_historical_ohlcv(ticker, days=BACKTEST_DAYS + 30)
    if df is None or len(df) < 30:
        return 0

    df = df.dropna(subset=['close', 'volume'])
    scenarios = 0
    log_ids = []  # (log_id, buy_date_idx, buy_price) — 사후 결과 업데이트용

    # 과거 데이터를 날짜 순으로 재생 (look-ahead bias 방지: 그 날까지만 사용)
    for i in range(20, len(df) - 5):
        hist = df.iloc[:i+1]  # 그 날까지의 데이터만
        today_row = hist.iloc[-1]
        price = float(today_row['close'])
        trade_date = hist.index[-1].strftime('%Y-%m-%d')

        # 기술 지표 계산 (그 날까지 데이터로만)
        close_s = hist['close']
        rsi = None
        if len(close_s) >= 16:
            try:
                rsi = round(float(calc_rsi(close_s, 14).iloc[-1]), 1)
            except Exception:
                pass

        # RSI 신호 감지 (매수: 30 이하, 매도: 70 이상) — 시나리오 발생 조건
        signal = None
        if rsi is not None:
            if rsi <= 32:
                signal = 'BUY'
            elif rsi >= 68:
                signal = 'SELL'

        if signal is None:
            continue  # 신호 없는 날은 건너뜀

        # 이동평균
        sma5 = float(close_s.rolling(5).mean().iloc[-1]) if len(close_s) >= 5 else 0
        sma20 = float(close_s.rolling(20).mean().iloc[-1]) if len(close_s) >= 20 else 0
        vol_avg = float(hist['volume'].rolling(20).mean().iloc[-1]) if len(hist) >= 20 else 0
        vol_today = float(today_row['volume'])
        vol_ratio = (vol_today / vol_avg * 100) if vol_avg > 0 else 0

        context = (
            f"[백테스트] 날짜: {trade_date} | 현재가: {price:,.0f}원\n"
            f"RSI(14): {rsi} | 5일선: {sma5:,.0f}원 | 20일선: {sma20:,.0f}원\n"
            f"거래량: 평소 대비 {vol_ratio:.0f}%"
        )

        # AI 판단 요청
        try:
            result = claude_client.ai_approve_trade(
                signal, stock_name, ticker, price, 'RSI백테스트',
                rsi or 0, [],
                [],  # 백테스트에선 과거 매매 이력 없음
                load_ai_rules(user_id),
                context=context,
                portfolio_context="[백테스트 모드] 실제 포트폴리오 없음"
            )
            decision, reason = result[0], result[1]
            confidence = result[2] if len(result) > 2 else 75
        except Exception as e:
            logger.debug(f"[백테스트] {ticker} AI 판단 오류: {e}")
            time.sleep(2)
            continue

        # 로그 기록
        log_id = log_ai_decision(
            user_id=user_id, mode='KR', ticker=ticker, stock_name=stock_name,
            signal=signal, ai_decision='CONFIRM' if decision else 'REJECT',
            confidence=confidence, ai_reason=reason[:300],
            input_context=context, portfolio_snapshot='',
            market_regime='N/A', strategy='RSI백테스트',
            price=price, session_type='backtest'
        )

        # 사후 결과: 5거래일 후 종가로 수익률 계산
        if i + 5 < len(df):
            future_price = float(df.iloc[i + 5]['close'])
            pnl_pct = (future_price / price - 1) * 100
            pnl = (future_price - price) * 1  # 1주 기준
            update_ai_decision_outcome(log_id, future_price, pnl, pnl_pct, 5)

        scenarios += 1
        # API 레이트 리밋 방지
        time.sleep(0.5)

    return scenarios


class BacktestRunner:
    """야간 백테스트 스케줄러."""

    def __init__(self, user_id: int, claude_client, toss_api=None):
        self.user_id = user_id
        self.claude = claude_client
        self.toss = toss_api
        self._running = False

    def run_nightly_batch(self):
        """오늘 배치 실행 — DAILY_BATCH_SIZE개 종목 처리."""
        from base.database import update_backtest_progress, get_backtest_pending_tickers

        logger.info("[백테스트] 야간 배치 시작")
        all_tickers = _get_all_kr_tickers()
        if not all_tickers:
            logger.warning("[백테스트] 종목 리스트 조회 실패")
            return

        # 이미 완료한 종목 제외, 안 한 종목 우선
        done_set = get_backtest_pending_tickers('KR')
        pending = [t for t in all_tickers if t['ticker'] not in done_set]
        if not pending:
            # 전체 완료 → 처음부터 다시 (최신 데이터로 재순환)
            logger.info("[백테스트] 전체 순환 완료 — 처음부터 재시작")
            pending = all_tickers

        batch = pending[:DAILY_BATCH_SIZE]
        total_scenarios = 0

        for item in batch:
            ticker = item['ticker']
            name = item['name']
            try:
                n = run_backtest_for_ticker(
                    ticker, name, self.user_id, self.claude, self.toss
                )
                if n > 0:
                    update_backtest_progress('KR', ticker, name,
                                             datetime.now().strftime('%Y-%m-%d'), n)
                    total_scenarios += n
                    logger.info(f"[백테스트] {name}({ticker}): {n}개 시나리오 완료")
            except Exception as e:
                logger.warning(f"[백테스트] {name}({ticker}) 오류: {e}")
            time.sleep(1)

        logger.info(f"[백테스트] 야간 배치 완료 — {len(batch)}종목 / {total_scenarios}시나리오")
        return total_scenarios
