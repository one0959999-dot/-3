"""
백테스트 단독 실행 스크립트
봇(매매 로직) 없이 백테스트 데이터 수집만 수행합니다.

사용법:
  python run_backtest.py              # KR + US 무한 반복
  python run_backtest.py --mode KR    # KR만
  python run_backtest.py --mode US    # US만
  python run_backtest.py --once       # 배치 1회만 실행 후 종료
"""

import sys
import os
import time
import logging
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('backtest_standalone.log', encoding='utf-8'),
    ]
)
logger = logging.getLogger('lassi_bot')


def _load_user(conn):
    row = conn.execute('SELECT * FROM users LIMIT 1').fetchone()
    if not row:
        logger.error("DB에 유저가 없습니다. 먼저 웹에서 회원가입하세요.")
        sys.exit(1)
    return dict(row)


def _build_ai(user: dict):
    gemini_key = user.get('gemini_api_key') or ''
    if gemini_key:
        from ai.gemini_api import GeminiApi
        logger.info("[AI] Gemini Flash 사용 (무료 티어)")
        return GeminiApi(gemini_key)
    claude_key = user.get('claude_api_key') or ''
    from ai.claude_api import ClaudeApi
    logger.info("[AI] Claude 사용 (Gemini API 키 없음)")
    return ClaudeApi(claude_key)


def _build_toss(user: dict):
    try:
        from base.toss_api import TossApi
        toss = TossApi(
            client_id=user.get('toss_client_id') or '',
            client_secret=user.get('toss_client_secret') or '',
            account_seq=user.get('toss_account_seq') or '',
        )
        return toss
    except Exception as e:
        logger.warning(f"토스 API 초기화 실패 (yfinance fallback 사용): {e}")
        return None


def run_kr(user: dict, once: bool = False):
    from KR.backtest_runner import BacktestRunner, BATCH_SIZE_WEEKEND
    ai   = _build_ai(user)
    toss = _build_toss(user)
    fred = user.get('fred_api_key') or ''
    runner = BacktestRunner(user['id'], ai, toss_api=toss, fred_key=fred)

    batch = BATCH_SIZE_WEEKEND
    logger.info(f"[KR 백테스트] 배치 크기: {batch}종목")

    while True:
        try:
            n = runner.run_batch(batch)
            logger.info(f"[KR 백테스트] 완료 — {n}개 신호 저장")
        except Exception as e:
            logger.error(f"[KR 백테스트] 오류: {e}", exc_info=True)
        if once:
            break
        logger.info("[KR 백테스트] 다음 배치까지 5분 대기…")
        time.sleep(300)


def run_us(user: dict, once: bool = False):
    from US.backtest_runner import USBacktestRunner, BATCH_SIZE_WEEKEND
    ai     = _build_ai(user)
    runner = USBacktestRunner(user['id'], ai)

    batch = BATCH_SIZE_WEEKEND
    logger.info(f"[US 백테스트] 배치 크기: {batch}종목")

    while True:
        try:
            n = runner.run_batch(batch)
            logger.info(f"[US 백테스트] 완료 — {n}개 신호 저장")
        except Exception as e:
            logger.error(f"[US 백테스트] 오류: {e}", exc_info=True)
        if once:
            break
        logger.info("[US 백테스트] 다음 배치까지 5분 대기…")
        time.sleep(300)


def main():
    parser = argparse.ArgumentParser(description='백테스트 단독 실행')
    parser.add_argument('--mode', choices=['KR', 'US', 'ALL'], default='ALL',
                        help='실행 대상 (기본: ALL)')
    parser.add_argument('--once', action='store_true',
                        help='배치 1회만 실행 후 종료')
    args = parser.parse_args()

    from base.database import init_db, get_db_connection
    init_db()

    conn = get_db_connection()
    user = _load_user(conn)
    conn.close()

    if not user.get('claude_api_key'):
        logger.error("Claude API 키가 없습니다. 웹 설정 → API 키를 먼저 입력하세요.")
        sys.exit(1)

    logger.info(f"=== 백테스트 단독 모드 시작 (mode={args.mode}, once={args.once}) ===")

    if args.mode == 'KR':
        run_kr(user, args.once)

    elif args.mode == 'US':
        run_us(user, args.once)

    else:
        import threading
        t_kr = threading.Thread(target=run_kr, args=(user, args.once), daemon=True, name='KR-backtest')
        t_us = threading.Thread(target=run_us, args=(user, args.once), daemon=True, name='US-backtest')
        t_kr.start()
        time.sleep(10)
        t_us.start()

        try:
            t_kr.join()
            t_us.join()
        except KeyboardInterrupt:
            logger.info("중단 요청 — 종료합니다.")


if __name__ == '__main__':
    main()
