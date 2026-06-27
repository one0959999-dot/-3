"""모멘텀 top4 라이브 전략 — 월 1회 실행, 자립형(백테스트/국면판단/점수제도 불필요).

확정안(사용자 결정):
- 종목 풀: 코스피+코스닥 혼합 대형/중형 (아래 UNIVERSE)
- 신호: 최근 12개월(252거래일) 수익률 = 모멘텀
- 선정: 상위 4종목, 균등분할, 순수 모멘텀(하락 방어 없음)
- 端수 처리: 종목당 예산으로 1주도 못 사면 다음 순위로 채움(살 수 있는 4종목 유지)
- 월 1회(첫 거래일) 리밸런싱: top4 밖 종목 매도 → top4 매수

안전: 기본 DRY-RUN(주문 안 하고 '무엇을 할지'만 출력/텔레그램). 실제 주문은 --live 일 때만.
자립형: yfinance(시세) + toss_api(주문/잔고) + telegram 만 필요. backtest·국면·점수 코드 불필요.

실행:
  python KR/momentum_strategy.py            # 드라이런(이번 달 top4 + 매매계획 미리보기)
  python KR/momentum_strategy.py --live     # 실제 주문(토스 계좌)
  python KR/momentum_strategy.py --telegram # 계획을 텔레그램으로
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import datetime as dt
import numpy as np
import pandas as pd

TOP_K = 6               # 과최적화 회피: 분산으로 MDD↓(238·61 표본 공통 robust). top4→top6
LOOKBACK = 252          # 12개월 모멘텀(표준·양 표본서 robust. 6M은 표본의존이라 회피)
SKIP_RECENT = 0         # (옵션) 최근 N일 제외 모멘텀. 0=순수 12개월

# ── 종목 풀: 코스피+코스닥 혼합 대형/중형 (유동성 큰 것 위주) ──
UNIVERSE = [
    # 코스피 대형
    ('005930', '삼성전자'), ('000660', 'SK하이닉스'), ('373220', 'LG에너지솔루션'),
    ('207940', '삼성바이오로직스'), ('005380', '현대차'), ('000270', '기아'),
    ('005490', 'POSCO홀딩스'), ('035420', 'NAVER'), ('035720', '카카오'),
    ('051910', 'LG화학'), ('006400', '삼성SDI'), ('068270', '셀트리온'),
    ('105560', 'KB금융'), ('055550', '신한지주'), ('086790', '하나금융'),
    ('012330', '현대모비스'), ('066570', 'LG전자'), ('003670', '포스코퓨처엠'),
    ('015760', '한국전력'), ('017670', 'SK텔레콤'), ('033780', 'KT&G'),
    ('010130', '고려아연'), ('009150', '삼성전기'), ('316140', '우리금융'),
    ('259960', '크래프톤'), ('011200', 'HMM'), ('042700', '한미반도체'),
    ('012450', '한화에어로스페이스'), ('010140', '삼성중공업'), ('009540', 'HD한국조선해양'),
    ('034020', '두산에너빌리티'), ('042660', '한화오션'), ('267260', 'HD현대일렉트릭'),
    ('064350', '현대로템'), ('047810', '한국항공우주'), ('051900', 'LG생활건강'),
    ('090430', '아모레퍼시픽'), ('097950', 'CJ제일제당'), ('271560', '오리온'),
    ('096770', 'SK이노베이션'), ('011070', 'LG이노텍'), ('018260', '삼성에스디에스'),
    ('032830', '삼성생명'), ('000810', '삼성화재'), ('138040', '메리츠금융지주'),
    ('010950', 'S-Oil'), ('011780', '금호석유'), ('112610', '씨에스윈드'),
    ('010120', 'LS일렉트릭'), ('352820', '하이브'),
    # 코스닥 대형/중형
    ('247540', '에코프로비엠'), ('086520', '에코프로'), ('196170', '알테오젠'),
    ('028300', 'HLB'), ('058470', '리노공업'), ('240810', '원익IPS'),
    ('357780', '솔브레인'), ('000250', '삼천당제약'), ('263750', '펄어비스'),
    ('293490', '카카오게임즈'), ('068760', '셀트리온제약'),
]
KOSDAQ = {'247540', '086520', '196170', '028300', '058470', '240810',
          '357780', '000250', '263750', '293490', '068760', '101490'}


def _yf_symbol(code):
    return code + ('.KQ' if code in KOSDAQ else '.KS')


def fetch_prices(codes):
    """yfinance로 최근 ~14개월 종가. {code: close_series}."""
    import yfinance as yf
    syms = [_yf_symbol(c) for c in codes]
    start = (dt.date.today() - dt.timedelta(days=460)).isoformat()
    raw = yf.download(syms, start=start, progress=False, auto_adjust=True, group_by='ticker')
    out = {}
    for c in codes:
        s = _yf_symbol(c)
        try:
            ser = raw[s]['Close'].dropna() if isinstance(raw.columns, pd.MultiIndex) else raw['Close'].dropna()
            if len(ser) >= LOOKBACK + 5:
                out[c] = ser
        except Exception:
            pass
    return out


def compute_momentum(prices):
    """각 종목 12개월 수익률(=모멘텀). SKIP_RECENT 적용 가능."""
    mom = {}
    for c, ser in prices.items():
        if len(ser) < LOOKBACK + SKIP_RECENT + 1:
            continue
        p_now = ser.iloc[-1 - SKIP_RECENT]
        p_then = ser.iloc[-1 - SKIP_RECENT - LOOKBACK]
        if p_then > 0:
            mom[c] = (p_now / p_then - 1) * 100
    return dict(sorted(mom.items(), key=lambda kv: kv[1], reverse=True))


def latest_price(prices, code):
    return float(prices[code].iloc[-1]) if code in prices else None


def plan_rebalance(holdings, cash, prices, momentum, name_map):
    """현 보유+현금 → top_K(살 수 있는 것 우선)로 리밸런싱 계획.
    holdings: {code: shares}. 반환: (sells[], buys[], picks[])."""
    ranked = list(momentum.keys())                      # 모멘텀 내림차순 전체
    # 보유 평가액
    held_val = sum(s * (latest_price(prices, c) or 0) for c, s in holdings.items())
    investable = cash + held_val
    slot = investable / TOP_K
    # 살 수 있는 종목으로 top_K 채우기(모멘텀 순)
    picks = []
    for c in ranked:
        p = latest_price(prices, c)
        if p and p <= slot:                              # 1주 이상 살 수 있어야
            picks.append(c)
        if len(picks) == TOP_K:
            break
    pickset = set(picks)
    # 매도: 보유 중 picks 아닌 것 전량
    sells = [(c, holdings[c]) for c in holdings if c not in pickset and holdings[c] > 0]
    # 매도 후 현금 추정
    cash_after = cash + sum(s * (latest_price(prices, c) or 0) for c, s in sells)
    # 매수: picks 중 목표비중까지(이미 보유분 차감)
    buys = []
    for c in picks:
        p = latest_price(prices, c)
        if not p:
            continue
        have = holdings.get(c, 0)
        target_sh = int(slot // p)
        add = target_sh - have
        if add > 0:
            cost = add * p
            if cost <= cash_after:
                buys.append((c, add)); cash_after -= cost
            else:
                aff = int(cash_after // p)
                if aff > 0:
                    buys.append((c, aff)); cash_after -= aff * p
    return sells, buys, picks


def fmt_plan(sells, buys, picks, prices, momentum, name_map, investable):
    L = [f"📈 모멘텀 top{TOP_K} 리밸런싱 계획 ({dt.date.today().isoformat()})",
         f"투자가능 ≈ {investable/1e4:,.0f}만원 · 12개월 모멘텀 기준", ""]
    L.append("[이번 달 선정 top]")
    for i, c in enumerate(picks, 1):
        L.append(f"  {i}. {name_map.get(c,c)}({c})  모멘텀 {momentum.get(c,0):+.0f}%  현재가 {latest_price(prices,c):,.0f}원")
    L.append("")
    L.append("[매도]" if sells else "[매도] 없음")
    for c, s in sells:
        L.append(f"  - {name_map.get(c,c)}({c}) {s}주")
    L.append("[매수]" if buys else "[매수] 없음")
    for c, s in buys:
        L.append(f"  + {name_map.get(c,c)}({c}) {s}주 (~{s*latest_price(prices,c):,.0f}원)")
    return "\n".join(L)


def _make_toss():
    """DB users 자격증명으로 TossInvestApi 구성(봇과 동일 로직, is_mock으로 실/모의)."""
    import sqlite3
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lassi.db')
    c = sqlite3.connect(db, timeout=30); c.row_factory = sqlite3.Row
    r = c.execute("SELECT * FROM users WHERE (toss_client_id IS NOT NULL AND toss_client_id!='') "
                  "OR (real_app_key IS NOT NULL AND real_app_key!='') LIMIT 1").fetchone()
    c.close()
    if not r:
        raise RuntimeError("토스 자격증명 없음(users.toss_client_id)")
    r = dict(r)
    mock = bool(r.get('is_mock', 1))
    g = lambda a, b: (r.get(a) or r.get(b) or '')
    cid = g('toss_client_id', 'us_app_key' if mock else 'real_app_key')
    sec = g('toss_client_secret', 'us_app_secret' if mock else 'real_app_secret')
    seq = g('toss_account_seq', 'us_account_no' if mock else 'real_account_no')
    from base.toss_api import TossInvestApi
    return TossInvestApi(cid, sec, seq)


def run(live=False, telegram=False, capital=None):
    name_map = {c: n for c, n in UNIVERSE}
    codes = [c for c, n in UNIVERSE]
    print("시세 수집 중...")
    prices = fetch_prices(codes)
    momentum = compute_momentum(prices)
    print(f"  모멘텀 계산 {len(momentum)}종목")

    # 잔고 조회: 실거래/실잔고는 토스, 미리보기는 --capital 가정 가능
    holdings, cash = {}, 0.0
    toss = None
    if live or capital is None:
        try:
            toss = _make_toss()
            bal = toss.get_account_balance()
            cash = float(bal.get('total_cash', 0) or 0)
            for s in bal.get('stocks', []):
                if int(s.get('shares', 0)) > 0:
                    holdings[s['ticker']] = int(s['shares'])
            print(f"  잔고: 현금 {cash:,.0f}원, 보유 {len(holdings)}종목")
        except Exception as e:
            print(f"  ⚠️ 잔고조회 불가({e})")
            toss = None
    if not holdings and cash == 0 and capital is not None:
        cash = float(capital)
        print(f"  (미리보기 가정: 투자금 {cash:,.0f}원, 보유 없음)")

    investable = cash + sum(s * (latest_price(prices, c) or 0) for c, s in holdings.items())
    sells, buys, picks = plan_rebalance(holdings, cash, prices, momentum, name_map)
    plan = fmt_plan(sells, buys, picks, prices, momentum, name_map, investable)
    print("\n" + plan)

    if telegram:
        try:
            import sqlite3
            db = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lassi.db')
            c = sqlite3.connect(db, timeout=30)
            r = c.execute("SELECT telegram_token, telegram_chat_id FROM users WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone()
            c.close()
            if r:
                from base.telegram_bot import TelegramNotifier
                TelegramNotifier(r[0], r[1]).send_message(("[실거래]" if live else "[미리보기]") + "\n" + plan)
                print("  텔레그램 전송 ✓")
        except Exception as e:
            print(f"  텔레그램 실패: {e}")

    if live:
        if toss is None:
            print("❌ 토스 연결 없음 — 실거래 불가"); return
        print("\n⚡ 실거래 실행...")
        for c, s in sells:
            ok = toss.sell_market_order(c, s)
            print(f"  매도 {name_map.get(c,c)} {s}주: {'✓' if ok else '✗'}")
        for c, s in buys:
            ok = toss.buy_market_order(c, s)
            print(f"  매수 {name_map.get(c,c)} {s}주: {'✓' if ok else '✗'}")
    else:
        print("\n(드라이런 — 실제 주문 없음. 실행하려면 --live)")


if __name__ == '__main__':
    cap = None
    for a in sys.argv:
        if a.startswith('--capital='):
            cap = float(a.split('=')[1])
    run(live='--live' in sys.argv, telegram='--telegram' in sys.argv, capital=cap)
