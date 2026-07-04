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
from KR.auto_order import load_toss, read_account, main as run_order, tg, quarter_tag, read_reb
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
    print(f"[계좌] 현금 {cash/1e4:,.0f}만, 보유 {len(holdings)}종목, 총 {total/1e4:,.0f}만")

    # 현금 없으면 대기 (조용히)
    if cash < MIN_CASH:
        print(f"현금 {cash/1e4:,.0f}만 < {MIN_CASH//10000}만 — 신규자금 없음, 대기")
        return 0

    if not execute:
        print(f"📋 [드라이런] 현금 {cash/1e4:,.0f}만 감지 — 실행하면 이 돈 전액으로 알고리즘 매수. (--execute 필요)")
        tg(f"💡 신규자금 {cash/1e4:,.0f}만 감지 (드라이런). 자동투입 대기 중.")
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

    # 현금 생김 → 신규자금 전액 배분 매수 (auto_order buy-only, anytime=상시허용)
    cap_txt = f"{max_buy/1e4:,.0f}만" if max_buy else "전액"
    tg(f"🟢 신규자금 {cash/1e4:,.0f}만 감지 — 알고리즘 자동매수 시작 (투입 {cap_txt})")
    rc = run_order(execute=True, max_buy=max_buy, force=False, probe=False, budget_override=None, anytime=True)
    if rc == 0:
        mark_today()
    return rc


if __name__ == '__main__':
    a = sys.argv[1:]
    mx = int(a[a.index('--max') + 1]) if '--max' in a else None
    sys.exit(main('--execute' in a, mx))
