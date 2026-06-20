"""
stock_screener.py
멀티팩터 위성 종목 자동 스크리너
─────────────────────────────────
① 섹터/테마 강세 탐지  : KRX 섹터지수 → 현재 어떤 분야가 강한지 파악
② 거래량 급등 감지      : 최근 5일 거래량 vs 60일 평균 → 테마 수급 포착
③ 모멘텀 필터          : 최근 20일 수익률 플러스 종목만
④ 전략 백테스트        : 13가지 전략 중 최고 수익 전략 & 수익률 계산
⑤ 종합 점수로 랭킹      : 섹터보너스 + 거래량 점수 + 백테스트 수익률
"""

import time
import threading
import warnings
import logging

logger = logging.getLogger('lassi_bot')  # [BUG-FIX] NameError 방지

# W-05: 싱글턴 레이스컨디션 방지용 전역 락
_dl_predictor_lock = threading.Lock()
_dl_predictor_instance = None

# 당일 전종목 OHLCV 캐시 (pykrx 일괄 조회 — 30분마다 갱신)
_full_ticker_cache: dict = {'ts': 0.0, 'movers': [], 'all': []}
_full_ticker_lock  = threading.Lock()
_FULL_TICKER_TTL   = 1800   # 30분 (장중 급등주 포착 주기)

# 모듈 레벨 AI 클라이언트 — select_satellites() 호출 시 자동 주입됨
# kr_bot.py가 select_ai_core_stock()에 gemini를 전달하지 않으므로
# select_satellites()에서 먼저 받아 여기에 저장 → 코어 선정 시 재사용
_module_claude = None

# 미국 섹터 선행 지수 캐시 (yfinance — 4시간마다 갱신)
_us_sector_cache: dict = {'ts': 0.0, 'boosts': {}}
_us_sector_lock  = threading.Lock()
_US_SECTOR_TTL   = 14400  # 4시간 (미국장 마감 → KR장 개장 사이클 커버)

# 당일 위성 후보 기준
# 위성: 완만한 상승 추세 (0.3% ~ 3%) → 중기 홀딩, 한달 20% 목표
_MOVER_CHG_MIN  = 0.3           # 등락률 최소 0.3% — 완전히 멈춘 종목 제외
_MOVER_CHG_MAX  = 3.0           # 등락률 3% 이상은 급등주 → 제외
_MOVER_VAL_MIN  = 500_000_000   # 거래대금 5억 이상 → 유동성 확보 종목

from pykrx import stock
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

warnings.filterwarnings('ignore')

EXCLUDE_TICKERS = {"003850"}
NUM_SATELLITES  = 5
BACKTEST_DAYS   = 130

def _last_biz_day(days_back=0):
    """최근 영업일 날짜 문자열 반환"""
    d = datetime.today() - timedelta(days=days_back)
    for _ in range(10):
        s = d.strftime("%Y%m%d")
        try:
            df = stock.get_market_ohlcv_by_date(s, s, "005930")
            if not df.empty:
                return s
        except Exception:
            pass
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")

_ohlcv_cache: dict = {}   # {(ticker, days): (date_str, DataFrame)}
_cache_lock  = threading.Lock()
_OHLCV_CACHE_MAX = 500    # 최대 캐시 항목 수 — 초과 시 오래된 절반 제거

def fetch_ohlcv(ticker, days=200, toss=None):
    today_str = datetime.today().strftime('%Y%m%d')
    key = (ticker, days)

    # 오늘 날짜의 캐시가 있으면 그대로 반환 (Thread-Safe)
    with _cache_lock:
        if key in _ohlcv_cache and _ohlcv_cache[key][0] == today_str:
            return _ohlcv_cache[key][1]

    try:
        if toss is not None:
            time.sleep(0.25)  # 모의투자 API 초당 4회 제한 대응
            df = toss.get_ohlcv(ticker, "D")
            if df is not None and not df.empty:
                result = df.dropna(subset=['close']).tail(days)
                with _cache_lock:
                    if len(_ohlcv_cache) >= _OHLCV_CACHE_MAX:
                        drop_keys = list(_ohlcv_cache.keys())[:_OHLCV_CACHE_MAX // 2]
                        for k in drop_keys:
                            del _ohlcv_cache[k]
                    _ohlcv_cache[key] = (today_str, result)
                return result

        # KIS API 미설정 또는 실패 시 pykrx 백업
        end   = datetime.today()
        start = end - timedelta(days=days + 90)
        time.sleep(0.05)  # API Rate limit 방어
        df = stock.get_market_ohlcv_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker
        )
        df.rename(columns={
            '시가':'open','고가':'high','저가':'low',
            '종가':'close','거래량':'volume'
        }, inplace=True)
        result = df.dropna(subset=['close']).tail(days)

        with _cache_lock:
            # 캐시 크기 상한 초과 시 오래된 절반 제거 (단순 FIFO 방식)
            if len(_ohlcv_cache) >= _OHLCV_CACHE_MAX:
                drop_keys = list(_ohlcv_cache.keys())[:_OHLCV_CACHE_MAX // 2]
                for k in drop_keys:
                    del _ohlcv_cache[k]
            _ohlcv_cache[key] = (today_str, result)
        return result
    except Exception:
        return pd.DataFrame()


# ──────────────────────────────────────────────
# 2. 섹터/테마 강세 탐지 (종목 수익률 기반)
# ──────────────────────────────────────────────
# 섹터별 대표 종목 목록 (pykrx 지수 API 불안정으로 직접 정의)
# 섹터별 대표 종목 — 섹터 강세 측정용 (10→30개로 확대)
# 대형주·중형주·소형주 혼합 — 틀린 종목코드는 pykrx에서 조용히 스킵됨
SECTOR_STOCKS = {
    "반도체": [
        "005930","000660","042700","091990","336370","000990","357780","240810","029460","058470",
        "403870","140860","005290","039030","319660","166090","131290","089030","183300","108320",
        "285550","074600","102710","014680","348210","036810","079370","285130","078860","232350",
    ],
    "2차전지": [
        "373220","006400","051910","247540","096770","011790","003670","066970","277070","382800",
        "086520","278280","259930","032350","336260","011070","009830","042670","361610","264450",
        "298050","025560","020120","082740","180640","298040","282690","048260","025900","006280",
    ],
    "바이오/제약": [
        "068270","207940","000120","003850","128940","326030","196170","145020","302440","263750",
        "068760","009420","185750","069620","009290","141080","086450","214450","039200","028300",
        "215360","048410","013030","003060","091420","078640","017900","218410","253450","219080",
    ],
    "자동차": [
        "005380","000270","012330","204320","009150","073240","241560","015260","064350","018880",
        "011210","046890","023810","010690","015750","019680","161390","086280","004490","095570",
        "013360","040610","038500","200880","001530","003460","054630","000500","006260","015290",
    ],
    "IT/소프트웨어": [
        "035420","035720","259960","112040","047050","293490","251270","036570","263750","095660",
        "035900","078340","060310","030200","053800","042110","045390","041510","064760","039030",
        "078130","053210","053060","222080","086670","041920","078600","307950","041900","048830",
    ],
    "방산/우주": [
        "012450","047810","004830","272210","079550","013890","064350","071970","298040","000880",
        "006650","016380","003570","004770","018250","012030","034810","026890","105630","155660",
        "003480","073440","069080","006110","241560","138040","009540","010140","042660","329180",
    ],
    "조선/중공업": [
        "009540","010140","042660","329180","267270","138040","034020","005440","003670","241560",
        "009180","014440","009280","004560","002630","023460","017800","006490","003690","009310",
        "000240","074260","065130","001940","014530","024040","067830","073520","004870","006360",
    ],
    "금융/보험": [
        "055550","105560","086790","000810","316140","175330","024110","032830","003450","139130",
        "071050","138930","010620","016360","001500","000640","002290","029780","014790","003540",
        "005940","039490","030610","001450","085620","006120","037270","203450","006800","016610",
    ],
    "에너지/화학": [
        "010950","011170","096770","267250","078930","001570","010060","006360","161390","011790",
        "009830","010120","042670","034730","036460","006110","011820","002350","004000","001040",
        "051600","011190","120110","120010","014915","271560","001680","025900","005720","003070",
    ],
    "건설/부동산": [
        "000720","047040","028260","034020","006360","294870","047050","000840","003450","012630",
        "023960","015360","018290","012690","013570","004990","014780","023350","034810","070590",
        "025870","012200","022100","003460","006260","009405","001230","005960","010780","097230",
    ],
    "유통/소비": [
        "139480","023530","004170","282330","016360","069960","011780","007070","088350","084680",
        "035510","007310","004980","002270","002790","027740","033180","031430","039130","145670",
        "000080","013000","007410","010130","025000","001740","004060","001680","069960","006400",
    ],
    "AI/로봇": [
        "017670","042700","079550","108860","285490","950130","336260","377300","348370","438900",
        "030190","240810","036570","035420","035720","293490","078340","042110","257690","094860",
        "064760","036540","032080","047110","217270","066570","078130","030190","108860","285490",
    ],
    "전력/전기": [
        "015760","267260","010120","298040","009470","117580","053080","175330","298020","064760",
        "267270","010140","010060","036460","001680","051600","011690","105630","001790","025820",
        "000100","001040","009830","003230","017960","006110","001060","010580","023090","001800",
    ],
}

def get_sector_momentum(lookback=20, verbose=False):
    """
    섹터별 대표 종목 수익률 평균으로 강세 섹터 탐지.

    ▶ 개선: 20일(중기) + 5일(단기) 듀얼 모멘텀 합산.
      - 20일 모멘텀 70% 가중 + 5일 모멘텀 30% 가중.
      - 하락장에서 최근 반등 중인 섹터를 5일 모멘텀이 끌어올려 잡아냄.

    Returns: dict { sector_name: blended_return_pct }  (내림차순 정렬)
    """
    results = {}
    end   = datetime.today()
    # 20일 조회를 위해 +30일 여유 확보 (공휴일 제외)
    start = end - timedelta(days=lookback + 30)

    for sector_name, tickers in SECTOR_STOCKS.items():
        rets_long  = []  # 20일 수익률
        rets_short = []  # 5일 수익률
        for t in tickers:
            if not t.isdigit() or len(t) != 6:
                continue
            try:
                df = stock.get_market_ohlcv_by_date(
                    start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), t
                )
                if df is None or len(df) < 5:
                    continue
                col = '종가' if '종가' in df.columns else df.columns[3]
                series = df[col].dropna()
                if len(series) >= 5:
                    # 20일 (중기) 모멘텀
                    ret_long = (series.iloc[-1] / series.iloc[-min(lookback, len(series)-1)] - 1) * 100
                    rets_long.append(float(ret_long))
                    # 5일 (단기) 모멘텀 — 최근 반등 포착
                    ret_short = (series.iloc[-1] / series.iloc[-min(5, len(series)-1)] - 1) * 100
                    rets_short.append(float(ret_short))
            except Exception:
                continue
        if rets_long:
            avg_long  = float(np.mean(rets_long))
            avg_short = float(np.mean(rets_short)) if rets_short else avg_long
            # 듀얼 모멘텀: 20일 70% + 5일 30% 가중 평균
            blended = avg_long * 0.70 + avg_short * 0.30
            results[sector_name] = round(blended, 2)

    sorted_results = dict(sorted(results.items(), key=lambda x: x[1], reverse=True))
    if verbose:
        print("\n📊 현재 섹터/테마 강세 분석 (20일×0.7 + 5일×0.3 블렌디드 수익률)")
        for name, ret in sorted_results.items():
            bar = "▲" if ret > 0 else "▼"
            print(f"   {bar} {name:<18} {ret:+.1f}%")
    return sorted_results


def get_sector_tickers(momentum, top_n_sectors=4):
    """
    강세 섹터 상위 N개 → 해당 섹터 대표 종목 반환.
    같은 종목이 여러 섹터에 중복 등록된 경우 먼저 등장한 섹터에만 할당 (점수 중복 방지).

    ▶ 개선: 절대 수익률 양수 조건(v > 0) 제거 →  상대 강세 기준으로 변경.
      - 양수 섹터가 2개 이상이면 양수 섹터 중 상위 N개 우선 선택.
      - 전반적 하락장(양수 섹터 1개 이하)이면 -10% 이상인 섹터 중 상위 N개 선택
        (절대 수익률이 낮아도 상대적으로 덜 빠진 섹터 = 상대 강세).
    ▶ 반환값에 ticker_sector_rank 추가 (0=1위 섹터, 1=2위 섹터, ...)
       → select_satellites()에서 1위 섹터 보너스를 더 높게 책정하는 데 사용.
    Returns: set of tickers, dict of ticker→sector_name, list of hot sector names, dict of ticker→rank
    """
    sorted_sectors = sorted(momentum.items(), key=lambda x: x[1], reverse=True)

    # ▶ 순수 상대 강세 기준: 항상 상위 top_n_sectors개 선택 (절대 수익률 무관)
    # 단, 섹터 보너스는 실제 수익률 품질에 따라 caller(select_satellites)에서 차등 적용
    hot_sector_items = sorted_sectors[:top_n_sectors]
    hot_sectors = [k for k, v in hot_sector_items]
    # 섹터 수익률 맵 (보너스 차등화용으로 반환)
    hot_sector_returns = {k: v for k, v in hot_sector_items}

    sector_tickers    = set()
    ticker_to_sector  = {}
    ticker_sector_rank = {}  # 섹터 순위 (0=최강세)
    seen = set()  # 중복 종목 방지

    for rank, sec_name in enumerate(hot_sectors):
        tickers = SECTOR_STOCKS.get(sec_name, [])
        for t in tickers:
            if t.isdigit() and len(t) == 6 and t not in EXCLUDE_TICKERS and t not in seen:
                sector_tickers.add(t)
                ticker_to_sector[t] = sec_name
                ticker_sector_rank[t] = rank
                seen.add(t)

    return sector_tickers, ticker_to_sector, hot_sectors, ticker_sector_rank, hot_sector_returns


# ──────────────────────────────────────────────
# 3. 거래량 급등 감지
# ──────────────────────────────────────────────
def _get_all_listed_tickers() -> tuple:
    """KOSPI+KOSDAQ 당일 전종목 OHLCV 일괄 조회 — 30분 캐싱.

    Returns: (movers, all_sorted)
      movers     : 등락률 >= 1.5% OR 거래대금 >= 5억 종목 전부 (개수 무제한 — 급등주 누락 방지)
      all_sorted : 전체 종목 등락률 내림차순 (폴백용)
    """
    now = time.time()
    with _full_ticker_lock:
        if now - _full_ticker_cache['ts'] < _FULL_TICKER_TTL and _full_ticker_cache['movers']:
            return list(_full_ticker_cache['movers']), list(_full_ticker_cache['all'])

    movers = []
    all_sorted = []
    try:
        today = datetime.today().strftime('%Y%m%d')
        frames = []
        for market in ('KOSPI', 'KOSDAQ'):
            try:
                df = stock.get_market_ohlcv_by_ticker(today, market=market)
                if df is not None and not df.empty:
                    frames.append(df)
            except Exception:
                pass

        if frames:
            all_df = pd.concat(frames)
            chg_col = next((c for c in ['등락률', '수익률', 'change'] if c in all_df.columns), None)
            val_col = next((c for c in ['거래대금', 'value']           if c in all_df.columns), None)

            if chg_col and val_col:
                all_df = all_df.sort_values([chg_col, val_col], ascending=[False, False])
                # 위성 후보: 0.3~3% 상승 OR 거래대금 5억↑ (단, 3%↑ 급등주는 제외 → 모멘텀 슬롯 전담)
                mask = (all_df[chg_col] < _MOVER_CHG_MAX) & (
                    (all_df[chg_col] >= _MOVER_CHG_MIN) | (all_df[val_col] >= _MOVER_VAL_MIN)
                )
                mover_df = all_df[mask]
                movers = [t for t in mover_df.index if isinstance(t, str) and t.isdigit() and len(t) == 6]
            elif val_col:
                all_df = all_df.sort_values(val_col, ascending=False)

            all_sorted = [t for t in all_df.index if isinstance(t, str) and t.isdigit() and len(t) == 6]
            logger.info(f"[스크리너] pykrx 전종목 갱신: 급등후보 {len(movers)}개 / 전체 {len(all_sorted)}개")
        else:
            # 장 시작 전 / pykrx 당일 데이터 없음 → 종목 목록만 폴백
            kospi  = list(stock.get_market_ticker_list(today, market='KOSPI'))
            kosdaq = list(stock.get_market_ticker_list(today, market='KOSDAQ'))
            all_sorted = [t for t in kospi + kosdaq if isinstance(t, str) and t.isdigit() and len(t) == 6]
            movers = []
            logger.info(f"[스크리너] pykrx 장전 폴백: {len(all_sorted)}개")

    except Exception as e:
        logger.warning(f"[스크리너] pykrx 전종목 조회 실패: {e}")

    with _full_ticker_lock:
        _full_ticker_cache['ts']     = time.time()
        _full_ticker_cache['movers'] = movers
        _full_ticker_cache['all']    = all_sorted
    return list(movers), list(all_sorted)


def get_candidate_tickers(toss=None, verbose=False):
    """
    KOSPI+KOSDAQ 후보 종목 풀 생성.
    - 방법1: KIS API 동적 거래량/등락률 상위 종목 수집
    - 방법2: 알려진 주요 종목 풀 + 섹터 대표 종목
    - 방법3: pykrx 전체 상장 종목에서 랜덤 보완 (매 실행 200개 — 반복 실행 시 전체 시장 커버)
    """
    # 알려진 주요 종목 풀 (KOSPI 대형주 + KOSDAQ 대형주 + 각 섹터 대표)
    BASE_POOL = [
        # KOSPI 대형주
        "005930","000660","005380","005490","028260","015760","066570","086790",
        "032830","055550","105560","012330","000270","096770","009150","010950",
        "011170","034020","078930","000810","316140","047050","033780","003550",
        "207940","068270","326030","128940","009540","010140","042660","329180",
        "373220","006400","051910","247540","011790","012450","047810","079550",
        "267270","138040","035420","035720","259960","112040","139480","023530",
        "004170","282330","016360","069960","017670","108860","285490","000720",
        "047040","034020","006360","294870","175330","000120","001570","267250",
        # KOSDAQ 대형주
        "293490","950130","263750","322000","214150","145020","091990","336370",
        "035900","041510","024060","066970","086520","357780","096530","347860",
        "272210","013890","004830","042700","035900","041510","091990",
        "196170","251270","064350","101400","236200","036540","263720",
    ]

    # 섹터별 종목도 추가
    for tickers in SECTOR_STOCKS.values():
        for t in tickers:
            if t.isdigit() and len(t) == 6:
                BASE_POOL.append(t)

    # 우선순위 순서: ① KIS 실시간 → ② pykrx 당일 급등 → ③ BASE_POOL(고정 폴백)
    # 이 순서로 max_scan을 자르면 급등주가 앞에 있어 누락 위험 최소화
    seen = set()
    unique = []

    # 1. 토스 실시간 상위 (가장 빠른 신호)
    dynamic_count = 0
    if toss is not None:
        if verbose:
            print("   🌐 토스 API 실시간 거래량/등락률 상위 종목 수집 중...")
        try:
            top_kospi      = toss.get_volume_rank(market_div="J", limit=30)
            top_kosdaq     = toss.get_volume_rank(market_div="Q", limit=30)
            top_rise_kospi = toss.get_price_change_rank(market_div="J", limit=20)
            top_rise_kosdaq= toss.get_price_change_rank(market_div="Q", limit=20)
            for t in top_kospi + top_kosdaq + top_rise_kospi + top_rise_kosdaq:
                if t not in seen and t not in EXCLUDE_TICKERS:
                    seen.add(t); unique.append(t); dynamic_count += 1
            if verbose:
                print(f"   ✨ 토스 실시간 {dynamic_count}개")
        except Exception as e:
            if verbose:
                print(f"   ⚠️ KIS 실시간 수집 실패: {e}")

    # 2. pykrx 당일 급등 — 조건 통과 종목 전부 (개수 제한 없음, 급등주 누락 방지)
    try:
        movers, all_sorted = _get_all_listed_tickers()
        mover_added = 0
        for t in movers:
            if t not in seen and t not in EXCLUDE_TICKERS:
                seen.add(t); unique.append(t); mover_added += 1

        # 장 전 / 데이터 없는 경우 폴백: 등락률순 200개
        fallback_added = 0
        if not movers:
            for t in all_sorted:
                if fallback_added >= 200: break
                if t not in seen and t not in EXCLUDE_TICKERS:
                    seen.add(t); unique.append(t); fallback_added += 1

        if verbose:
            if mover_added:
                print(f"   🚀 pykrx 급등 후보 {mover_added}개 (무제한)")
            elif fallback_added:
                print(f"   🗂️  pykrx 폴백 {fallback_added}개")
    except Exception as e:
        logger.warning(f"[스크리너] pykrx 보완 실패: {e}")

    # 3. BASE_POOL — 위에서 빠진 대형주/섹터 대표 보완 (정적 폴백)
    for t in BASE_POOL:
        if t not in seen and t not in EXCLUDE_TICKERS:
            seen.add(t)
            unique.append(t)

    return unique


def get_volume_surge_tickers(toss=None,
                              market_list=("KOSPI", "KOSDAQ"),
                              surge_ratio=1.8,
                              min_cap_billion=300,
                              max_tickers=150,
                              max_scan=500,
                              verbose=False):
    """
    거래량 급등 종목 필터.
    - surge_ratio: 최근5일 평균거래량 / 60일 평균거래량 > surge_ratio
    - max_scan: OHLCV 분석 최대 종목 수 (후보는 중요도순 정렬 — 상위가 핵심)
    Returns: dict { ticker: volume_score }
    """
    tickers = get_candidate_tickers(toss=toss, verbose=verbose)
    # 하드 캡: 급등 활황일에도 max_scan 이내로 제한 (서버 보호)
    # 후보 순서 = KIS 실시간 → BASE_POOL → pykrx 급등 조건 → 중요도 높은 순
    if len(tickers) > max_scan:
        tickers = tickers[:max_scan]
        if verbose:
            print(f"   ⚡ 서버 부하 방지: 상위 {max_scan}개만 분석")
    candidates = {}

    if verbose:
        print(f"   후보 풀 {len(tickers)}개 종목 거래량 분석 중...")

    for ticker in tickers:
        if ticker in EXCLUDE_TICKERS:
            continue
        try:
            # 💡 days=80 대신 백테스트 기간인 BACKTEST_DAYS(130)로 일치시켜 캐시된 데이터를 재사용하게 만듭니다.
            df = fetch_ohlcv(ticker, days=BACKTEST_DAYS, toss=toss)
            if len(df) < 30 or 'volume' not in df.columns:
                continue
            if df['close'].iloc[-1] < 1000:
                continue
            # 일평균 거래대금 3억 미만 제외 — 유동성/시총 최소 기준 (min_cap_billion 프록시)
            if 'volume' in df.columns:
                avg_trading_value = (df['close'].iloc[-5:] * df['volume'].iloc[-5:]).mean()
                if avg_trading_value < 300_000_000:
                    continue
            vol_recent = df['volume'].iloc[-5:].mean()
            vol_base   = df['volume'].iloc[-75:-5].mean()
            if vol_base < 100:
                continue
            ratio = vol_recent / (vol_base + 1e-9)
            if ratio >= surge_ratio:
                candidates[ticker] = round(float(ratio), 2)
        except Exception:
            continue

    sorted_c = dict(sorted(candidates.items(), key=lambda x: x[1], reverse=True))
    if verbose:
        print(f"\n📈 거래량 급등 종목: {len(sorted_c)}개 발견 (기준 {surge_ratio}x 이상)")
        for t, r in list(sorted_c.items())[:10]:
            try:
                name = stock.get_market_ticker_name(t)
            except Exception:
                name = t
            print(f"   {name}({t}): {r:.1f}x")
    return sorted_c



# ──────────────────────────────────────────────
# 4. 기술적 지표 & 전략 (기존 유지)
# ──────────────────────────────────────────────
def ema(s, n):    return s.ewm(span=n, adjust=False).mean()
def sma(s, n):    return s.rolling(n).mean()

def calc_rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / (l + 1e-10))


# ──────────────────────────────────────────────────────────────────────
# 미국 섹터 선행 지수 → KR 섹터 보너스
# ──────────────────────────────────────────────────────────────────────
# US 섹터 ETF → KR 섹터 매핑 (가중치 0~1.0)
# 한국 시장은 미국 야간 수익률에 높은 상관성 (반도체 0.85↑, 바이오 0.7, 방산 0.6)
_US_ETF_TO_KR_SECTORS: dict = {
    "SOXX": [("반도체", 1.0)],                              # 필라델피아 반도체 ETF
    "XLK":  [("IT/소프트웨어", 0.8), ("AI/로봇", 0.7)],     # 기술주 ETF
    "LIT":  [("2차전지", 1.0)],                             # 리튬/배터리 ETF
    "IBB":  [("바이오/제약", 1.0)],                         # 바이오텍 ETF
    "XLV":  [("바이오/제약", 0.6)],                         # 헬스케어 ETF (IBB 보완)
    "XLE":  [("에너지/화학", 0.9)],                         # 에너지 ETF
    "XLF":  [("금융/보험", 1.0)],                           # 금융 ETF
    "ITA":  [("방산/우주", 1.0)],                           # 방산/항공 ETF
    "XLY":  [("유통/소비", 0.8), ("자동차", 0.5)],          # 소비재 ETF
    "XLI":  [("조선/중공업", 0.7)],                         # 산업재 ETF
    "XLU":  [("전력/전기", 1.0)],                           # 유틸리티 ETF
    "XLRE": [("건설/부동산", 1.0)],                         # 부동산 ETF
}


def get_us_sector_boost(verbose: bool = False) -> dict:
    """
    미국 섹터 ETF 수익률을 분석해 KR 섹터별 선행 지수 보너스 반환.

    - 전일 미국장 수익률(1일) × 0.6 + 5일 추세 × 0.4 블렌드
    - 4시간 캐시 → KR 개장 전 미리 반영, 장중에는 재계산 없음

    Returns: { kr_sector_name: boost_score(float) }
      예) {"반도체": 12.0, "바이오/제약": -4.0, ...}
    """
    now_ts = time.time()
    with _us_sector_lock:
        if now_ts - _us_sector_cache['ts'] < _US_SECTOR_TTL and _us_sector_cache['boosts']:
            return dict(_us_sector_cache['boosts'])

    boosts: dict = {}
    try:
        import yfinance as yf
        etf_list = list(_US_ETF_TO_KR_SECTORS.keys())

        raw = yf.download(etf_list, period="10d", interval="1d",
                          progress=False, auto_adjust=True)
        if raw is None or raw.empty:
            return {}

        if isinstance(raw.columns, pd.MultiIndex):
            lv0 = raw.columns.get_level_values(0).unique().tolist()
            closes = raw["Close"] if "Close" in lv0 else raw.xs("Close", axis=1, level=1)
        else:
            closes = raw

        # ETF별 1일·5일 수익률 → 블렌디드 수익률
        etf_returns: dict = {}
        for etf in etf_list:
            try:
                if etf not in closes.columns:
                    continue
                c = closes[etf].dropna()
                if len(c) < 2:
                    continue
                ret_1d = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100
                ret_5d = (float(c.iloc[-1]) / float(c.iloc[-min(6, len(c)-1)]) - 1) * 100
                # 1일 60% + 5일 40% : 단기 반응 + 추세 둘 다 반영
                etf_returns[etf] = ret_1d * 0.6 + ret_5d * 0.4
            except Exception:
                continue

        # KR 섹터별 가중 평균 집계
        kr_sector_raw: dict = {}
        for etf, kr_sectors in _US_ETF_TO_KR_SECTORS.items():
            if etf not in etf_returns:
                continue
            for kr_sector, weight in kr_sectors:
                kr_sector_raw.setdefault(kr_sector, []).append(etf_returns[etf] * weight)

        # 블렌디드 수익률 → 보너스 점수 변환
        # 한국시장 US 연동 상관계수 반영: 반도체·2차전지 강함, 건설·유통 약함
        for kr_sector, vals in kr_sector_raw.items():
            avg = sum(vals) / len(vals)
            if avg >= 2.0:
                boost = 12.0
            elif avg >= 1.0:
                boost = 8.0
            elif avg >= 0.3:
                boost = 4.0
            elif avg >= -0.3:
                boost = 0.0
            elif avg >= -1.0:
                boost = -4.0
            else:
                boost = -8.0
            boosts[kr_sector] = boost

        if verbose:
            print("\n🇺🇸 [미국 섹터 선행 지수] KR 섹터 보너스 반영:")
            for sec, b in sorted(boosts.items(), key=lambda x: x[1], reverse=True):
                arrow = "📈" if b > 0 else ("📉" if b < 0 else "➡️")
                print(f"   {arrow} {sec:<16}: {b:+.0f}점")

    except Exception as e:
        logger.warning(f"[스크리너] 미국 섹터 선행 지수 조회 실패: {e}")
        return {}

    with _us_sector_lock:
        _us_sector_cache['ts']     = time.time()
        _us_sector_cache['boosts'] = boosts

    return boosts


def calc_signal_readiness(df: 'pd.DataFrame', strategy_name: str) -> float:
    """선정된 전략 기준으로 현재 BUY 신호까지의 '거리'를 점수화.

    - 신호 임박(5포인트 이내) : +20점
    - 신호 접근 중(~15포인트)  : +12점
    - 신호 멀지만 진행 중      :  +5점
    - 이미 크로스/신호 통과    :  -8점  (다음 신호까지 오래 기다려야 함)
    - 계산 실패               :   0점 (중립)
    """
    try:
        c = df['close']
        if len(c) < 30:
            return 0.0

        if 'RSI' in strategy_name:
            threshold = 40 if '40/60' in strategy_name else 30
            period    = 9  if 'RSI(9)' in strategy_name else 14
            rsi_cur   = float(calc_rsi(c, period).iloc[-1])
            rsi_prev  = float(calc_rsi(c, period).iloc[-2])
            gap = rsi_cur - threshold
            trending_down = rsi_cur < rsi_prev  # RSI 하락 중이면 신호 임박 가능성↑
            if gap <= 5:
                return 20.0
            elif gap <= 15:
                return 14.0 if trending_down else 10.0
            elif gap <= 30:
                return 6.0  if trending_down else 3.0
            else:
                return -8.0  # RSI 60 이상 — 한참 기다려야

        elif strategy_name in ('EMA 5/20 크로스', 'SMA 5/20 크로스',
                               'EMA 3/10 크로스', 'SMA 3/10 크로스', 'SMA 3/20 크로스'):
            use_ema = strategy_name.startswith('EMA')
            parts   = strategy_name.split()[1].split('/')
            fp, sp  = int(parts[0]), int(parts[1])
            if use_ema:
                fast = c.ewm(span=fp, adjust=False).mean()
                slow = c.ewm(span=sp, adjust=False).mean()
            else:
                fast = c.rolling(fp).mean()
                slow = c.rolling(sp).mean()
            f_cur, s_cur = float(fast.iloc[-1]), float(slow.iloc[-1])
            f_prv, s_prv = float(fast.iloc[-2]), float(slow.iloc[-2])
            if s_cur <= 0:
                return 0.0
            gap_pct = (s_cur - f_cur) / s_cur * 100  # 양수 = fast 아직 아래 = 크로스 대기
            gap_shrinking = (s_prv - f_prv) > (s_cur - f_cur)  # 갭 좁아지는 중?
            if gap_pct > 0:
                # 아직 데드크로스 상태 → 골든크로스 기대
                if gap_pct <= 1.0:
                    return 20.0
                elif gap_pct <= 3.0:
                    return 14.0 if gap_shrinking else 10.0
                elif gap_pct <= 6.0:
                    return 6.0  if gap_shrinking else 2.0
                else:
                    return -5.0  # 갭 너무 큼
            else:
                # 이미 골든크로스 완료 — 다음 사이클까지 신호 없음
                return -8.0

        elif strategy_name == 'MACD 크로스':
            macd_line, sig_line = calc_macd(c)
            hist_cur  = float((macd_line - sig_line).iloc[-1])
            hist_prev = float((macd_line - sig_line).iloc[-2])
            if hist_cur < 0:
                # 데드크로스 상태: 히스토그램 축소 중이면 골든크로스 임박
                shrinking = hist_cur > hist_prev
                if hist_cur > -50:
                    return 18.0 if shrinking else 8.0
                elif hist_cur > -200:
                    return 8.0  if shrinking else 2.0
                else:
                    return -5.0
            else:
                # 이미 골든크로스
                return -8.0

        elif strategy_name == '볼린저밴드 반전':
            bb_up, bb_mid, bb_low = calc_bb(c)
            bb_1sig = bb_mid - (bb_mid - bb_low) * 0.5  # -1σ 근사
            p = float(c.iloc[-1])
            if p <= float(bb_low.iloc[-1]) * 1.02:
                return 20.0   # 하단 터치 임박/돌파
            elif p <= float(bb_1sig.iloc[-1]):
                return 10.0   # -1σ 아래
            elif p <= float(bb_mid.iloc[-1]):
                return 3.0    # 중간선 아래
            else:
                return -8.0   # 중간선 위 — 하단 한참 멀었음

    except Exception:
        pass
    return 0.0

def calc_macd(s, f=12, sl=26, sig=9):
    m = ema(s, f) - ema(s, sl)
    return m, ema(m, sig)

def calc_bb(s, p=20, k=2):
    mid = sma(s, p); sd = s.rolling(p).std()
    return mid + k*sd, mid, mid - k*sd

def calc_stoch(h, l, c, kp=21, dp=5):
    # %K 기간 14→21, %D 3→5: 신호 과다 발생(150일에 60회) 방지
    lo = l.rolling(kp).min(); hi = h.rolling(kp).max()
    k  = 100 * (c - lo) / (hi - lo + 1e-10)
    return k, k.rolling(dp).mean()

def calc_cci(h, l, c, p=20):
    tp = (h + l + c) / 3; ma = sma(tp, p)
    md = tp.rolling(p).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - ma) / (0.015 * md + 1e-10)

def calc_williams(h, l, c, p=14):
    return -100 * (h.rolling(p).max() - c) / (h.rolling(p).max() - l.rolling(p).min() + 1e-10)

def cross_sig(fast, slow):
    s = pd.Series(0, index=fast.index)
    s[fast > slow] = 1; s[fast < slow] = -1
    t = s.diff().fillna(0)
    out = pd.Series(0, index=s.index)
    out[t > 0] = 1; out[t < 0] = -1
    return out

def threshold_sig(ind, lo, hi):
    s = pd.Series(0, index=ind.index)
    s[ind < lo] = 1; s[ind > hi] = -1
    prev = 0
    for i in s.index:
        if s[i] == prev: s[i] = 0
        elif s[i] != 0: prev = s[i]
    return s

STRATEGY_REGISTRY = {
    "RSI(9) 30/70":     lambda df: threshold_sig(calc_rsi(df['close'], 9), 30, 70),
    # RSI(14) 30/70 제거 — RSI(9)가 신호 빈도 2.5배·수익률 우위 (백테스트 확인)
    # "RSI(14) 30/70":  lambda df: threshold_sig(calc_rsi(df['close'], 14), 30, 70),
    "RSI(14) 40/60":    lambda df: threshold_sig(calc_rsi(df['close'], 14), 40, 60),
    "EMA 5/20 크로스":   lambda df: cross_sig(ema(df['close'], 5), ema(df['close'], 20)),
    "EMA 3/10 크로스":   lambda df: cross_sig(ema(df['close'], 3), ema(df['close'], 10)),
    "SMA 5/20 크로스":   lambda df: cross_sig(sma(df['close'], 5), sma(df['close'], 20)),
    "SMA 3/10 크로스":   lambda df: cross_sig(sma(df['close'], 3), sma(df['close'], 10)),
    "SMA 3/20 크로스":   lambda df: cross_sig(sma(df['close'], 3), sma(df['close'], 20)),
    "MACD 크로스":       lambda df: cross_sig(*calc_macd(df['close'])),
    "볼린저밴드 반전":    lambda df: threshold_sig(
                             df['close'] / calc_bb(df['close'])[1], 0.97, 1.03),
    "Stochastic 크로스": lambda df: cross_sig(*calc_stoch(df['high'], df['low'], df['close'])),
    "CCI ±100":          lambda df: threshold_sig(
                             calc_cci(df['high'], df['low'], df['close']), -100, 100),
    "Williams %R":       lambda df: threshold_sig(
                             calc_williams(df['high'], df['low'], df['close']), -80, -20),
}

def backtest(df, sig_series, initial=1_000_000):
    c = df['close']
    cash, holding, buy_price = float(initial), 0, 0
    fee_rate = 0.00015 # 0.015% 온라인 수수료
    tax_rate = 0.0018  # 0.18% 증권거래세
    
    for date in df.index:
        price = int(c.loc[date])
        sig   = sig_series.get(date, 0) if isinstance(sig_series, pd.Series) else 0
        
        # 매수 (수수료 가산)
        if sig == 1 and holding == 0 and cash >= price * (1 + fee_rate):
            cost_per_share = price * (1 + fee_rate)
            holding = int(cash // cost_per_share)
            if holding > 0:
                cash -= holding * cost_per_share
                buy_price = price
                
        # 매도 (수수료 및 세금 차감)
        elif sig == -1 and holding > 0:
            revenue_per_share = price * (1 - fee_rate - tax_rate)
            cash += holding * revenue_per_share
            holding = 0
            
    # 백테스트 종료 시 잔여 보유 물량 강제 청산 가치 계산
    if holding > 0:
        revenue_per_share = int(c.iloc[-1]) * (1 - fee_rate - tax_rate)
        cash += holding * revenue_per_share
        
    return (cash - initial) / initial * 100

def find_best_strategy(df):
    """
    Walk-forward 검증으로 과적합 방지.
    - 신호 생성: 전체 df (MA/RSI 이동평균 웜업을 위해 전체 필요)
    - 성능 평가: 마지막 30% 구간(OOS)만 사용
    - OOS 전 전략이 모두 손실이면 전체 기간 기준으로 폴백
    """
    if len(df) < 50:
        return None, -9999

    split = max(20, int(len(df) * 0.70))
    oos_df = df.iloc[split:]

    best_name, best_ret = None, -9999
    for name, fn in STRATEGY_REGISTRY.items():
        try:
            full_sig = fn(df)           # 전체 기간으로 신호 생성 (워밍업 포함)
            oos_sig  = full_sig.iloc[split:]
            # 과신호 전략 제외: OOS 기간(40일 내외)에 매수 신호 15개 초과 시 수수료 과다 예상
            if (oos_sig == 1).sum() > 15:
                continue
            ret = backtest(oos_df, oos_sig)
            if ret > best_ret:
                best_ret, best_name = ret, name
        except Exception:
            continue

    # OOS에서 모든 전략이 -30% 이하면 전체 기간 폴백 (데이터 부족 방어)
    if best_ret < -30:
        for name, fn in STRATEGY_REGISTRY.items():
            try:
                sig = fn(df)
                ret = backtest(df, sig)
                if ret > best_ret:
                    best_ret, best_name = ret, name
            except Exception:
                continue

    return best_name, best_ret


# ──────────────────────────────────────────────
# 5. 메인 선정 함수
# ──────────────────────────────────────────────
def select_satellites(toss=None, n=NUM_SATELLITES, verbose=True, claude_client=None, bear_mode=False, sector_guide: str = '', real_toss=None, exclude: set = None):
    """
    멀티팩터 위성 종목 선정 (딥러닝 PyTorch 확률 예측 엔진 연동 완료)

    Parameters
    ----------
    exclude : set, optional
        후보 풀에서 완전 제외할 티커 집합.
        이미 보유 중인 종목, 모멘텀 슬롯, 당일 AI 거절 블랙리스트 등을 넘기면
        첫 번째 AI(ai_select_satellites) 단계부터 제외되어 이중 필터링 낭비 방지.
    """
    # 모듈 레벨 AI 클라이언트 저장 — select_ai_core_stock()이 gemini 없이 호출될 때 재사용
    global _module_claude
    if claude_client is not None:
        _module_claude = claude_client

    if verbose:
        print("\n" + "="*60)
        print("  🔍 위성 종목 딥러닝 + 멀티팩터 스크리닝 시작")
        print("  ① 섹터/테마 강세 분석")
        print("  ② KOSPI+KOSDAQ 거래량 급등 탐지")
        print("  ③ 종목별 최적 전략 백테스트")
        print("  ④ PyTorch LSTM 인공지능 상승 확률 분석")
        print("="*60)

    sector_momentum = get_sector_momentum(lookback=20, verbose=verbose)
    # 전체 섹터 종목을 후보 풀에 포함 (강세 섹터는 보너스 점수만 부여, 필수 조건 아님)
    sector_tickers, ticker_to_sector, hot_sectors, ticker_sector_rank, hot_sector_returns = get_sector_tickers(sector_momentum, top_n_sectors=len(SECTOR_STOCKS))

    # 미국 섹터 선행 지수 사전 로드 (4시간 캐시 — 미장 마감 후 KR 개장 전에 한 번만 조회)
    us_sector_boosts = get_us_sector_boost(verbose=verbose)

    if verbose:
        if hot_sectors:
            sector_labels = []
            for sec in hot_sectors:
                ret = sector_momentum.get(sec, 0)
                quality = "🟢" if ret > 0 else ("🟡" if ret > -5 else "🔴")
                label = f"{quality}{sec}({ret:+.1f}%)"
                sector_labels.append(label)
            print(f"\n🔥 상대 강세 섹터 TOP4: {', '.join(sector_labels)}")
        else:
            print("\n⚠️  섹터 데이터 없음")

    # 거래량 임계치 1.3x — 한국 급등주 백테스트 분석 결과 평균 1.33x (기존 1.5에서 완화)
    volume_surges = get_volume_surge_tickers(
        toss=toss, market_list=("KOSPI", "KOSDAQ"),
        surge_ratio=1.3, min_cap_billion=300, max_tickers=150, verbose=verbose
    )

    # 외인/기관 순매수 팩터 수집
    # real_toss가 주입된 경우(모의봇) 실전 API로 데이터 조회, 없으면 toss 자체 사용
    _fi_toss = real_toss or toss
    frgn_inst_tickers = set()
    frgn_only_tickers = set()   # 161번: 외국계 전용 순매수 (037과 구분해 보너스 차별화)
    if _fi_toss is not None:
        # ① 037: 국내기관+외국인 합산 순매수 상위
        try:
            fi_kospi  = _fi_toss.get_foreign_institution_rank(market_div="J", limit=30)
            fi_kosdaq = _fi_toss.get_foreign_institution_rank(market_div="Q", limit=30)
            for item in fi_kospi + fi_kosdaq:
                if (item.get("frgn_ntby_qty", 0) > 0 or item.get("orgn_ntby_qty", 0) > 0):
                    frgn_inst_tickers.add(item["ticker"])
        except Exception:
            pass

        # ② 161: 외국계 증권사 전용 순매수 상위 (전체시장 기준, 금액순)
        try:
            if hasattr(_fi_toss, 'get_foreign_buy_rank'):
                fi_frgn = _fi_toss.get_foreign_buy_rank(market_div="0000", sort_by="0", limit=50)
                for item in fi_frgn:
                    if item.get("frgn_net_qty", 0) > 0:
                        t = item["ticker"]
                        frgn_only_tickers.add(t)
                        frgn_inst_tickers.add(t)   # 후보 풀에도 포함
        except Exception:
            pass

        if verbose:
            src = "(실전 API)" if real_toss else ""
            print(f"   💼 외인/기관 순매수(037): {len(frgn_inst_tickers) - len(frgn_only_tickers - frgn_inst_tickers)}개  "
                  f"외국계 전용(161): {len(frgn_only_tickers)}개 {src}")

    candidate_pool = set(volume_surges.keys()) | sector_tickers | frgn_inst_tickers
    candidate_pool -= EXCLUDE_TICKERS
    # 호출자가 명시적으로 제외 요청한 티커 제거 (보유 중·모멘텀 슬롯·당일 AI 거절 블랙리스트 등)
    if exclude:
        candidate_pool -= exclude

    if verbose:
        excl_note = f" (블랙리스트·보유 {len(exclude)}개 사전 제외)" if exclude else ""
        print(f"\n📋 후보 풀: 거래량 급등 {len(volume_surges)}개 + 강세 섹터 {len(sector_tickers)}개 + 외인기관 {len(frgn_inst_tickers)}개 → 합계 {len(candidate_pool)}개{excl_note}")

    # 🤖 딥러닝 모델 로드 (모듈 레벨 싱글턴 — 매 호출마다 디스크 I/O 방지)
    # W-05: 락으로 멀티스레드 환경에서 중복 생성(레이스컨디션) 방지
    # BUG-06 FIX: dl_model 임포트 실패 시 무음 처리되던 것을 명시적 경고로 변경
    try:
        from dl_model import DeepLearningPredictor
    except ImportError as e:
        logger.warning(f"[스크리너] dl_model 로드 실패 — DL 예측 비활성화: {e}")
        DeepLearningPredictor = None
    global _dl_predictor_instance
    if DeepLearningPredictor is not None and _dl_predictor_instance is None:
        with _dl_predictor_lock:
            if _dl_predictor_instance is None:  # double-checked locking
                try:
                    _dl_predictor_instance = DeepLearningPredictor()
                except Exception as e:
                    logger.warning(f"[스크리너] DeepLearningPredictor 초기화 실패: {e}")
    dl_predictor = _dl_predictor_instance  # None이면 아래에서 폴백 처리

    # ── Step 4: 후보별 백테스트 + 종합 점수 ──
    results = []
    processed = 0

    # 레버리지/인버스 ETF 이름 키워드 — 위성 포트폴리오 편입 금지
    _ETF_EXCLUDE_KEYWORDS = (
        "레버리지", "인버스", "2X", "3X", "2배", "3배",
        "선물인버스", "인버스(합성)", "레버리지(합성)",
    )

    for ticker in candidate_pool:
        try:
            name = stock.get_market_ticker_name(ticker)

            # ── 레버리지/인버스 ETF 자동 제외 ──────────────────────────
            if any(kw in name for kw in _ETF_EXCLUDE_KEYWORDS):
                logger.debug(f"[스크리너] ETF 제외: {name}({ticker})")
                continue

            df   = fetch_ohlcv(ticker, days=BACKTEST_DAYS, toss=toss)
            if len(df) < 40 or 'close' not in df.columns:
                continue

            recent_ret = (df['close'].iloc[-1] / df['close'].iloc[-min(20, len(df)-1)] - 1) * 100

            # BEAR 국면에서는 기준 완화 (-25%) — 하락장에서도 반등 후보 확보
            drawdown_threshold = -25 if bear_mode else -15
            if recent_ret < drawdown_threshold:
                continue

            bb_upper, bb_mid, bb_lower = calc_bb(df['close'])
            current_price = df['close'].iloc[-1]
            bb_discount = (bb_mid.iloc[-1] - current_price) / bb_mid.iloc[-1] * 100

            vol_score = volume_surges.get(ticker, 1.0)
            # 섹터 보너스: 순위 기반 × 품질 보정
            # - 순위 보너스: 1위 +22, 2위 +18, 3위 +14, 4위 +10
            # - 품질 보정: 섹터 수익률 양수(+1.0) / -5%까지(+0.6) / 그 이하(+0.3)
            #   → 하락장에서 덜 빠진 섹터는 절반 이하 보너스만 받음
            if ticker in sector_tickers:
                _rank    = ticker_sector_rank.get(ticker, 3)
                _sec_ret = hot_sector_returns.get(ticker_to_sector.get(ticker, ""), 0)
                _base    = max(22 - _rank * 4, 10)
                if _sec_ret > 0:
                    _quality = 1.0    # 절대 강세 — 전액 보너스
                elif _sec_ret > -5:
                    _quality = 0.6    # 약보합 — 60% 보너스
                else:
                    _quality = 0.3    # 하락장 상대강세 — 30% 보너스
                sector_bonus = int(_base * _quality)
            else:
                sector_bonus = 0

            # 미국 섹터 선행 지수 보너스 (전날 미국장 → KR 개장 방향 선반영)
            # 반도체·2차전지·바이오 등 미국 연동 섹터는 최대 ±12점 추가
            us_sector_name = ticker_to_sector.get(ticker, "")
            us_boost = us_sector_boosts.get(us_sector_name, 0.0)
            sector_bonus += us_boost

            # 외인/기관 보너스: 037(기관+외인) +8 / 161(외국계 전용) 추가 +5 = 최대 +13
            if ticker in frgn_only_tickers:
                frgn_inst_bonus = 13   # 외국계 전용 순매수 — 더 강한 신호
            elif ticker in frgn_inst_tickers:
                frgn_inst_bonus = 8    # 기관+외인 합산 순매수
            else:
                frgn_inst_bonus = 0

            # 20일 모멘텀 부스트 — 이미 오르고 있는 종목 우대 (한달 20% 목표)
            # 3~20% 범위: 적당한 상승 추세 → 최대 +16점 보너스
            # 20% 초과: 부스트 없음 (아래 과열 패널티로 이어짐)
            momentum_boost = 0.0
            if 3.0 <= recent_ret <= 20.0:
                momentum_boost = recent_ret * 0.8   # 3%→+2.4점, 10%→+8점, 20%→+16점

            # ── 52주 위치 점수 (한국 급등주 백테스트 분석 반영) ──────────────
            # 분석 결과: 한국 급등주 급등 직전 평균 52주 위치 79% (테마 진행 중)
            # 40~80%: 스윗스팟 → 보너스 / 15~40%: 저점 탈출 초기 → 소보너스
            # 92% 초과: 극단적 과열 → 소패널티
            pos_52w = None
            pos_52w_score = 0.0
            if 'high' in df.columns and 'low' in df.columns and len(df) >= 60:
                _n52 = min(252, len(df))
                high_52 = df['high'].tail(_n52).max()
                low_52  = df['low'].tail(_n52).min()
                pos_52w = float((current_price - low_52) / (high_52 - low_52 + 1e-9) * 100)
                if 40 <= pos_52w <= 80:
                    pos_52w_score = 5.0    # 스윗스팟: 테마 진행 중
                elif 15 <= pos_52w < 40:
                    pos_52w_score = 3.0    # 저점 탈출 초기
                elif pos_52w > 92:
                    pos_52w_score = -2.0   # 극단적 과열 소패널티

            # ── 과열 패널티 (테마 수혜 시 완화) ──────────────────────────────
            # 한국 분석: HLB·알테오젠 등 강한 테마 수혜주는 RSI 80~90에서도 추가 급등
            # → 섹터 보너스 10점 이상(강한 테마)이면 패널티 50% 감면
            overheated_penalty = 0
            if recent_ret > 15 and vol_score < 2.0:
                base_penalty   = (recent_ret - 15) * 1.5
                theme_discount = 0.5 if sector_bonus >= 10 else 1.0
                overheated_penalty = base_penalty * theme_discount

            stat_arb_penalty = 0
            if recent_ret > 30:
                stat_arb_penalty = (recent_ret - 30) * 0.8

            # dl_predictor가 None이면 중립 50.0으로 폴백 (DL 없이도 스크리닝 계속)
            ai_up_prob = dl_predictor.predict_up_probability(df) if dl_predictor is not None else 50.0
            ml_factor_score = (ai_up_prob - 50.0) * 0.2

            score = ((vol_score - 1) * 6
                     + sector_bonus
                     + frgn_inst_bonus
                     + (bb_discount * 1.5)
                     + momentum_boost          # 20일 모멘텀 부스트
                     + pos_52w_score           # 52주 위치 점수
                     - overheated_penalty
                     - stat_arb_penalty
                     + ml_factor_score
                     + rsi_oversold_bonus      # RSI 과매도 근접 보너스
                     + macd_pullback_bonus)    # MACD 눌림목 보너스

            # RSI(14) 현재값 계산 — AI 전략 검수 프롬프트에 활용
            try:
                rsi_val = round(float(calc_rsi(df['close'], 14).iloc[-1]), 1)
            except Exception:
                rsi_val = None

            # ── RSI 과매도 근접 보너스 (전략 무관 — 30 근처 종목 우선 선정) ──────
            # 선정 즉시 매수 신호와 연결되도록 RSI 30 임박 종목을 상위 배치.
            # calc_signal_readiness가 RSI 전략에만 적용되므로 여기서 공통 보완.
            rsi_oversold_bonus = 0.0
            if rsi_val is not None:
                if rsi_val <= 32:   rsi_oversold_bonus = 15.0   # 30 터치·직후
                elif rsi_val <= 38: rsi_oversold_bonus = 10.0   # 임박
                elif rsi_val <= 45: rsi_oversold_bonus = 4.0    # 접근 중

            # ── MACD 역추세 눌림목 보너스 (선정 단계) ────────────────────────
            # 데드크로스 구간 = 단기 눌림 = 선정 후 저점 진입 가능성 높음
            # 골든크로스 구간 = 이미 오른 뒤 선정 = 고점 진입 리스크
            macd_pullback_bonus = 0.0
            try:
                if len(df) >= 30:
                    _macd_line, _sig_line = calc_macd(df['close'])
                    _hist_now  = float((_macd_line - _sig_line).iloc[-1])
                    _hist_prev = float((_macd_line - _sig_line).iloc[-2])
                    if _hist_now < 0 and _hist_now > _hist_prev:
                        macd_pullback_bonus = 8.0   # 음수 구간 반등 중 = 최적 타이밍
                    elif _hist_now < 0:
                        macd_pullback_bonus = 4.0   # 단순 눌림목
                    # 골든크로스(hist > 0) → 0점 (이미 올랐을 가능성)
            except Exception:
                pass

            results.append({
                'ticker':       ticker,
                'name':         name,
                'return_pct':   0.0,
                'volume_surge': float(round(vol_score, 2)),
                'vol_ratio':    float(round(vol_score, 2)),
                'rsi':          rsi_val,
                'sector':       ticker_to_sector.get(ticker, '-'),
                'momentum_20d': float(round(recent_ret, 2)),
                'pos_52w':      float(round(pos_52w, 1)) if pos_52w is not None else None,
                'dl_prob':      float(round(ai_up_prob, 1)),
                'frgn_inst':    ticker in frgn_inst_tickers,
                'frgn_only':    ticker in frgn_only_tickers,
                'score':        float(round(score, 2)),
                'current_price': int(current_price),
            })
            processed += 1

            if verbose and processed % 10 == 0:
                print(f"   ... {processed}개 분석 완료")

        except Exception:
            continue

    results.sort(key=lambda x: x['score'], reverse=True)
    
    selected = None
    if claude_client:
        if verbose:
            print("\n🤖 [AI 자율 매매] Claude AI가 최종 위성 종목과 전략을 선정 중입니다...")
        ai_result = claude_client.ai_select_satellites(results, hot_sectors[:4], n, sector_guide=sector_guide)
        if ai_result:
            selected = ai_result
            if verbose: print("   ✅ AI 텍스트 선정 완료!")
        else:
            if verbose: print("   ⚠️ AI 선정 실패 (폴백: 기존 득점 순으로 선정)")

    if not selected:
        selected = results[:n]

    if verbose:
        print(f"\n{'='*60}")
        print(f"  ✅ 위성 종목 선정 완료! ({len(results)}개 중 상위 {n}개)")
        print(f"{'='*60}")
        for rank, c in enumerate(selected, 1):
            vol_tag = f"📈거래량{c.get('volume_surge', 1.0):.1f}x" if c.get('volume_surge', 1.0) > 1.5 else ""
            sec_tag = f"🔥{c.get('sector', '-')}" if c.get('sector', '-') != '-' else ""
            fi_tag = ("🌍외국계전용" if c.get('frgn_only') else "💼외인기관") if c.get('frgn_inst') else ""
            dl_tag = f" 🧠[상승확률: {c.get('dl_prob', 0):.1f}%]" if c.get('dl_prob', 0) > 0 else ""
            ai_tag = f" 🤖[AI 선정: {c.get('ai_reason', '')}]" if c.get('ai_selected') else ""
            
            pos_tag = f"52주{c['pos_52w']:.0f}%" if c.get('pos_52w') is not None else ""
            sr = c.get('signal_readiness', 0)
            sr_tag = (f"🟢신호임박({sr:+.0f})" if sr >= 10
                      else f"🟡신호접근({sr:+.0f})" if sr >= 0
                      else f"🔴신호대기({sr:+.0f})")
            _us_b = us_sector_boosts.get(c.get('sector', ''), 0.0)
            us_tag = (f"🇺🇸US{_us_b:+.0f}" if _us_b != 0 else "")
            print(f"\n  {rank}위. [{c['name']}] ({c['ticker']}){dl_tag}{ai_tag}")
            print(f"       전략: {c['strategy_name']}  /  6개월 수익: {c.get('return_pct', 0):+.1f}%")
            print(f"       20일 모멘텀: {c.get('momentum_20d', 0):+.1f}%  {pos_tag}  {vol_tag}  {sec_tag}  {fi_tag}  {us_tag}")
            print(f"       종합점수: {c.get('score', 0):.1f}점  {sr_tag}")
        print(f"{'='*60}\n")

    return selected, hot_sectors



# ──────────────────────────────────────────────
# 테스트용 실행
# ──────────────────────────────────────────────
if __name__ == '__main__':
    print("섹터 강세 분석만 먼저 테스트:")
    m = get_sector_momentum(verbose=True)
    print("\n전체 스크리닝 시작 (약 3~5분 소요):")
    # M-02: select_satellites()는 (candidates, hot_sectors) 2-tuple 반환
    selected_candidates, hot_sectors_list = select_satellites(n=5, verbose=True)
    print(f"\n강세 섹터: {hot_sectors_list}")
    print(f"선정 종목 {len(selected_candidates)}개:")
    for c in selected_candidates:
        print(f"  {c['name']}({c['ticker']}) | 점수 {c.get('score',0)} | 수익률 {c.get('return_pct',0):+.1f}%")

# ──────────────────────────────────────────────
# 6. AI 코어 장기 우량주 자동 선정 (트리플 코어용)
# ──────────────────────────────────────────────
def select_ai_core_stock(n: int = 2, exclude_tickers=None, verbose: bool = False) -> list:
    """
    미리 정의된 우량주 풀(SECTOR_STOCKS) 중에서
    안정적으로 우상향(60/120일 이평 정배열 + 120일 모멘텀)하는 상위 n개 종목을 선정.

    Parameters
    ----------
    n               : 반환할 AI 코어 종목 수 (기본 2)
    exclude_tickers : 사용자 지정 코어 등 제외할 티커 set/list
    verbose         : 진단 로그 출력 여부

    Returns
    -------
    list of dict  [{ 'ticker', 'name', 'strategy_name', 'return_pct', 'sector' }, ...]
    """
    if verbose:
        print(f"\n🔍 [AI 코어] 장기 우상향 우량주 탐색 시작 (목표 {n}개)...")

    # 제외 티커 집합 구성
    exclude = set(EXCLUDE_TICKERS)
    if exclude_tickers:
        if isinstance(exclude_tickers, (set, list, tuple)):
            exclude.update(str(t) for t in exclude_tickers)
        else:
            exclude.add(str(exclude_tickers))

    # 섹터→티커 역매핑 (섹터 분산용)
    ticker_to_sector_map: dict = {}
    for sec, sec_tickers in SECTOR_STOCKS.items():
        for t in sec_tickers:
            if t not in ticker_to_sector_map:
                ticker_to_sector_map[t] = sec

    # 후보 풀 구성
    candidates: set = set()
    for sec_tickers in SECTOR_STOCKS.values():
        for t in sec_tickers:
            if t.isdigit() and len(t) == 6 and t not in exclude:
                candidates.add(t)

    # 종목별 점수 계산
    scored: list = []
    for ticker in list(candidates):
        try:
            df = fetch_ohlcv(ticker, days=BACKTEST_DAYS)
            if len(df) < 120 or 'close' not in df.columns:
                continue

            close = df['close']
            sma_60  = close.rolling(60).mean()
            sma_120 = close.rolling(120).mean()

            curr_close  = float(close.iloc[-1])
            curr_sma60  = float(sma_60.iloc[-1])
            curr_sma120 = float(sma_120.iloc[-1])

            # 60일·120일 이평 정배열 필터 (현재가 > SMA60 > SMA120)
            if not (curr_close > curr_sma60 > curr_sma120):
                continue

            momentum_120d = (curr_close / float(close.iloc[-120]) - 1) * 100
            std_20        = float(close.pct_change().rolling(20).std().iloc[-1]) * 100

            # MACD 역추세 보너스 — 눌림목 종목을 상위 배치 (저점 진입 기회)
            macd_bonus = 0.0
            try:
                _ml, _sl = calc_macd(close)
                _h_now  = float((_ml - _sl).iloc[-1])
                _h_prev = float((_ml - _sl).iloc[-2])
                if _h_now < 0 and _h_now > _h_prev:
                    macd_bonus = 5.0   # 음수 구간 반등 = 최적 진입 타이밍
                elif _h_now < 0:
                    macd_bonus = 2.0   # 단순 눌림목
            except Exception:
                pass

            # RSI 저평가 보너스 — 과매도 구간 종목 우선
            rsi_bonus = 0.0
            try:
                _rsi = float(calc_rsi(close, 14).iloc[-1])
                if _rsi <= 35:   rsi_bonus = 6.0
                elif _rsi <= 45: rsi_bonus = 3.0
            except Exception:
                pass

            # 점수 = 120일 모멘텀 - 변동성 패널티 + MACD 눌림목 + RSI 저평가
            score = momentum_120d - (std_20 * 2) + macd_bonus + rsi_bonus
            scored.append((score, ticker))

        except Exception:
            continue

    # 내림차순 정렬
    scored.sort(key=lambda x: x[0], reverse=True)

    # ── AI 코어 선정 (Gemini가 있으면 AI가 장기누적 기준으로 최종 선택) ──
    # kr_bot.py가 gemini 없이 호출하므로, select_satellites()에서 주입된 _module_claude 사용
    if _module_claude is not None and scored:
        if verbose:
            print(f"\n🤖 [AI 코어 선정] 퀀트 통과 {len(scored)}개 → Claude AI가 '장기 누적 매수' 기준으로 선정 중...")
        # AI에게 넘길 후보 준비 (상위 200개, 이름 포함)
        ai_candidates = []
        for sc, t in scored[:200]:
            try:
                nm = stock.get_market_ticker_name(t)
            except Exception:
                nm = t
            sec = ticker_to_sector_map.get(t, '-')
            mom = (sc + 0.0)  # score ≒ 120일 모멘텀 - 변동성 패널티
            # MACD 눌림목 여부 계산 (AI에게 전달)
            _macd_state = '-'
            try:
                _df_c = fetch_ohlcv(t, days=60)
                if len(_df_c) >= 30:
                    _ml2, _sl2 = calc_macd(_df_c['close'])
                    _h2 = float((_ml2 - _sl2).iloc[-1])
                    _hp2 = float((_ml2 - _sl2).iloc[-2])
                    if _h2 < 0 and _h2 > _hp2:
                        _macd_state = '눌림반등(최적)'
                    elif _h2 < 0:
                        _macd_state = '눌림목'
                    else:
                        _macd_state = '골든크로스'
            except Exception:
                pass

            ai_candidates.append({
                'ticker':        t,
                'name':          nm,
                'sector':        sec,
                'momentum_120d': round(sc, 1),
                'score':         round(sc, 1),
                'sma_aligned':   'YES',   # 이 목록은 이미 정배열 필터 통과
                'strategy_name': '정배열 장기보유',
                'return_pct':    round(sc, 2),
                'macd_state':    _macd_state,
            })
        ai_result = _module_claude.ai_select_core_stocks(ai_candidates, n)
        if ai_result:
            if verbose:
                for i, c in enumerate(ai_result, 1):
                    print(f"   🏆 AI 코어 {i}위: {c['name']}({c['ticker']}) | 섹터: {c.get('sector','-')} | 이유: {c.get('ai_reason','')}")
            return ai_result
        if verbose:
            print("   ⚠️ AI 코어 선정 실패 — 퀀트 랭킹 폴백")

    # ── 퀀트 폴백 — 섹터 분산 (같은 섹터 2개 금지) ──
    result: list = []
    used_sectors: set = set()

    for score, ticker in scored:
        if len(result) >= n:
            break
        sec = ticker_to_sector_map.get(ticker, ticker)
        if sec in used_sectors:
            continue   # 같은 섹터 중복 제외
        used_sectors.add(sec)

        try:
            name = stock.get_market_ticker_name(ticker)
        except Exception:
            name = ticker

        result.append({
            'ticker':        ticker,
            'name':          name,
            'strategy_name': '정배열 장기보유',
            'return_pct':    round(score, 2),
            'sector':        sec,
        })
        if verbose:
            print(f"   🏆 퀀트 코어 {len(result)}위: {name}({ticker}) | 섹터: {sec} | 점수 {score:.1f}")

    if not result and verbose:
        print("   ⚠️ 정배열 조건 통과 종목 없음 — 재시도 필요")

    return result


# ──────────────────────────────────────────────
# 7. 일일 시장 분석 리포트 자동 생성
# ──────────────────────────────────────────────
def generate_daily_market_report(claude_client=None, verbose=False, news_context=None, toss=None):
    """
    코스피/코스닥 대리 지수(ETF) 및 주도 섹터 데이터를 활용하여 텍스트 리포트를 생성합니다.
    claude_client가 제공되면 AI 기반 분석을 수행합니다.
    """
    if verbose:
        print("\n📝 일일 시장 분석 리포트 생성 중...")

    raw_data_lines = []
    today_str = datetime.today().strftime('%Y년 %m월 %d일')
    
    # 1. 시장 방향성 데이터 수집
    indices = {
        "KOSPI (KODEX 200)": "069500",
        "KOSDAQ (KODEX KOSDAQ150)": "229200"
    }
    
    raw_data_lines.append(f"날짜: {today_str}")
    raw_data_lines.append("\n[주요 지수 데이터]")
    for name, ticker in indices.items():
        try:
            df = fetch_ohlcv(ticker, days=30)
            if len(df) < 20: continue
            
            close = df['close']
            sma_5 = close.rolling(5).mean().iloc[-1]
            sma_20 = close.rolling(20).mean().iloc[-1]
            current = close.iloc[-1]
            pct_change = (current / close.iloc[-2] - 1) * 100
            rsi_14 = calc_rsi(close, 14).iloc[-1]
            
            raw_data_lines.append(f"- {name}: 현재가 {current:,.0f}원 ({pct_change:+.2f}%), 5일평균 {sma_5:,.0f}, 20일평균 {sma_20:,.0f}, RSI {rsi_14:.1f}")
        except: pass
            
    # 2. 주도 섹터 데이터
    raw_data_lines.append("\n[섹터 모멘텀]")
    try:
        momentum = get_sector_momentum(lookback=10, verbose=False)
        for sec, val in list(momentum.items())[:5]:
            raw_data_lines.append(f"- {sec}: {val:+.1f}%")
    except: pass
        
    # 3. 거래량 급등 데이터 — 숫자만 아닌 실제 종목 리스트 전달
    volume_surges = {}
    volume_surge_details = []   # [{ticker, name, ratio}] — 봇에 저장용
    try:
        volume_surges = get_volume_surge_tickers(toss=toss, surge_ratio=2.0, verbose=False)
        _surge_lines = [f"- 거래량 2배 급증 종목: 총 {len(volume_surges)}개 (상위 30개 상세)"]
        for _t, _r in list(volume_surges.items())[:30]:
            try:
                from pykrx import stock as _krx
                _name = _krx.get_market_ticker_name(_t)
            except Exception:
                _name = _t
            _surge_lines.append(f"  · {_name}({_t}): {_r:.1f}배")
            volume_surge_details.append({"ticker": _t, "name": _name, "ratio": round(_r, 2)})
        raw_data_lines.append("\n[수급 특이사항 — 거래량 급증 종목 전체 목록]")
        raw_data_lines.extend(_surge_lines)
    except Exception:
        pass

    # 🚨 [신규 추가] 주요 종목의 실시간 네이버 뉴스 텍스트 컨텍스트 결합
    if news_context:
        raw_data_lines.append("\n[포트폴리오 주도주 실시간 주요 뉴스 헤드라인]")
        raw_data_lines.append(news_context)

    market_data_text = "\n".join(raw_data_lines)

    # Claude AI 활용 여부 결정
    if claude_client:
        if verbose: print("   🤖 Claude AI 분석 요청 중...")
        report_text = claude_client.analyze_market(market_data_text)
    else:
        # Fallback: 기존 룰 기반 리포트
        report_lines = [f"### 📊 Lassi Bot 시장 분석 리포트 ({today_str})"]
        report_lines.append("\n#### 📈 주요 지수 동향")
        report_lines.append(market_data_text.replace("[", "#### ").replace("-", "*"))
        report_lines.append("\n> AI 분석 기능이 비활성화되어 있습니다. Claude API 키를 등록하면 더 정교한 분석을 받을 수 있습니다.")
        report_text = "\n".join(report_lines)

    if verbose:
        print(report_text)
        
    return {
        "date": datetime.today().strftime('%Y-%m-%d'),
        "report_markdown": report_text,
        "volume_surge_details": volume_surge_details,   # 실제 종목 리스트 [{ticker, name, ratio}]
    }
