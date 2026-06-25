"""파생 통계 갱신 — backtest_trade_signals 에서 섹터×국면·계절성 통계를 재집계.

백테스트 데이터가 늘어난 뒤(백테스트 재실행 후) 실행하면 라이브 종목선정·AI판단이
최신 통계를 사용한다. 봇이 주 1회 자동 호출하지만, 수동 실행도 가능.

사용: python tools/refresh_derived_stats.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
from base.database import rebuild_sector_phase_stats, rebuild_seasonality_stats


def refresh_all():
    out = {}
    for mode in ('KR', 'US'):
        try:
            rebuild_sector_phase_stats(mode)
            rebuild_seasonality_stats(mode)
            out[mode] = 'OK'
        except Exception as e:
            out[mode] = f'ERR: {e}'
    return out


if __name__ == '__main__':
    print('파생통계 갱신:', refresh_all())
