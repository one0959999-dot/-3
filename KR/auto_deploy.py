"""신규자금 자동투입 — 계좌에 현금 생기면(현대차 매도·추가입금 등) 자동으로 알고리즘 매수.

동작(매일 크론): 계좌 현금 조회 → MIN_CASH 이상이면 → auto_order 신규자금배분(매수) 전액 실행.
완전자동(묻지않음). 안전장치: 정규장·하루1회·중복차단·관찰체결·데이터무결성(auto_order 재사용).
기존보유는 안 건드림(신규 현금만 매수). 분기 리밸런스 진행중(ACTIVE)이면 보류(현금은 리밸런스 몫).
투입한도는 하드코딩하지 않음 — 가용현금 전액이 기본, --max로만 선택적 제한.

크론(장중 매일, KST 10:30): 30 1 * * 1-5 cd /home/ubuntu/lassi_bot && venv/bin/python KR/auto_deploy.py --execute

실행: python KR/auto_deploy.py                  # 드라이런(현금감지만, 주문0)
      python KR/auto_deploy.py --execute        # 실투입(현금≥30만이면 전액)
      python KR/auto_deploy.py --execute --max 1000000   # (선택) 캡 100만으로 제한
"""
import sys, os, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import warnings
warnings.filterwarnings('ignore')
from KR.auto_order import (load_toss, read_account, main as run_order, tg, quarter_tag, read_reb,
                           dca_reserved, dca_tranche_due)
from KR.live_v1 import is_rebalance_week

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
DEPLOY_MARKER = P('auto_deploy_done.txt')
MIN_CASH = 300_000  # 이 이상 현금 있어야 투입(소액 노이즈 방지)


def done_today():
    try:
        return open(DEPLOY_MARKER).read().strip() == datetime.date.today().isoformat()
    except Exception:
        return False


def mark_today():
    tmp = DEPLOY_MARKER + '.tmp'
    open(tmp, 'w').write(datetime.date.today().isoformat())
    os.replace(tmp, DEPLOY_MARKER)


def main(execute=False, max_buy=None):
    toss, uid = load_toss()
    if toss is None:
        print("Toss 자격증명 없음"); return 1

    # 리밸런스 주간엔 이번 분기 리밸런스가 끝나기(DONE) 전까지 현금은 리밸런스 몫 — 보류.
    # (ACTIVE 마커가 스캔 완료 후에야 써지는 타이밍 레이스까지 커버 — 감사 3.
    #  부트스트랩(ETF 미보유) 계좌는 리밸런스가 즉시 DONE을 찍어주므로 여기 안 걸림.)
    rq, rstate = read_reb()
    reb_done = (rq == quarter_tag() and rstate == 'DONE')
    if is_rebalance_week() and not reb_done:
        print("리밸런스 주간(이번 분기 미완) — 신규자금 배분 보류, 리밸런스가 우선"); return 0
    if rq == quarter_tag() and rstate == 'ACTIVE':
        print("분기 리밸런스 진행중(ACTIVE) — 신규자금 배분 보류"); return 0

    acct = read_account(toss)
    if acct is None:
        msg = "⛔ 자동투입 중단: 계좌조회 실패"; print(msg); tg(msg); return 1
    holdings, cash, total = acct
    # 지수 DCA 예약분은 '이미 배분 결정된 미래 트랜치 몫' — 신규자금이 아님. 빼고 판단(매일 오인알림 방지).
    reserved = dca_reserved()
    new_cash = max(0, cash - reserved)
    tranche_due = dca_tranche_due()
    resv_txt = f" (지수DCA 예약 {reserved/1e4:,.0f}만 제외)" if reserved else ""
    print(f"[계좌] 현금 {cash/1e4:,.0f}만 → 신규 {new_cash/1e4:,.0f}만{resv_txt}, 보유 {len(holdings)}종목, 총 {total/1e4:,.0f}만")

    # 신규자금도 없고 이번 달 지수 트랜치도 도래 안 했으면 대기 (조용히)
    if new_cash < MIN_CASH and not tranche_due:
        print(f"신규 {new_cash/1e4:,.0f}만 < {MIN_CASH//10000}만, 트랜치 미도래 — 대기")
        return 0

    if not execute:
        why = f"신규자금 {new_cash/1e4:,.0f}만" + (" + 지수 트랜치 도래" if tranche_due else "")
        print(f"📋 [드라이런] {why} 감지 — 실행하면 알고리즘 자동배분(저변동 즉시·지수 DCA). (--execute 필요)")
        tg(f"💡 {why} 감지 (드라이런). 자동투입 대기 중.")
        return 0

    # 하루 1회 제한
    if done_today():
        print("오늘 이미 자동투입 실행됨"); return 0

    # 정규장 확인
    try:
        if not toss.is_kr_market_open():
            print("정규장 아님 — 다음 장중 시도"); return 0
    except Exception:
        pass

    # 신규자금/트랜치 도래 → 배분 매수 (auto_order buy-only, anytime=상시허용). 지수 DCA는 auto_order가 처리.
    cap_txt = f"{max_buy/1e4:,.0f}만" if max_buy else "전액"
    trg = f"신규자금 {new_cash/1e4:,.0f}만" + (" + 지수 트랜치" if tranche_due else "")
    tg(f"🟢 {trg} 감지 — 알고리즘 자동배분 시작 (저변동 즉시·지수 DCA, 투입 {cap_txt})")
    rc = run_order(execute=True, max_buy=max_buy, force=False, probe=False, budget_override=None, anytime=True)
    if rc == 0:
        mark_today()
    return rc


if __name__ == '__main__':
    a = sys.argv[1:]
    mx = int(a[a.index('--max') + 1]) if '--max' in a else None
    sys.exit(main('--execute' in a, mx))
