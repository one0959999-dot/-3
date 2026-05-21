# 미국장 전환 마이그레이션 플랜

> 작성일: 2026-05-21  
> 목표: `is_mock=True` 슬롯(한국 모의투자) → 미국 실전투자로 교체  
> 전제: 한국 실전(`is_mock=False`) 슬롯은 그대로 유지

---

## 전환 후 최종 구조

```
현재:
  왼쪽 (is_mock=False) → 한국 실전
  오른쪽 (is_mock=True)  → 한국 모의   ← 이걸 교체

전환 후:
  왼쪽 (is_mock=False) → 한국 실전     ← 그대로
  오른쪽 (is_mock=True)  → 미국 실전   ← 내부만 교체
```

---

## 브로커 인터페이스 (9개 메서드 — 어떤 증권사든 이것만 구현하면 됨)

```python
# kis_brokers/base_broker.py  ← 이 파일 먼저 만들 것
from abc import ABC, abstractmethod
import pandas as pd

class BaseBroker(ABC):
    @abstractmethod
    def buy_market_order(self, ticker: str, qty: int) -> bool: ...
    @abstractmethod
    def sell_market_order(self, ticker: str, qty: int, price=None) -> bool: ...
    @abstractmethod
    def get_account_balance(self) -> dict: ...
    @abstractmethod
    def get_current_price(self, ticker: str) -> float: ...
    @abstractmethod
    def get_ohlcv(self, ticker: str, period: str) -> pd.DataFrame: ...
    @abstractmethod
    def get_minute_candles(self, ticker: str, count: int) -> pd.DataFrame: ...
    @abstractmethod
    def get_realtime_price_data(self, ticker: str) -> dict: ...
    @abstractmethod
    def get_etf_price(self, ticker: str) -> dict: ...
    @abstractmethod
    def get_approval_key(self) -> str: ...
```

> 나중에 토스 API 나오면 이 인터페이스만 구현하면 base_bot.py 한 줄도 안 바꿔도 됨.

---

## 작업 단계

---

### STEP 1. 브로커 인터페이스 추상화 (1~2시간)

**목적**: 이후 모든 교체 작업의 기반

- [ ] `kis_brokers/base_broker.py` 생성 (위 코드 기준)
- [ ] `KisRealApi(BaseBroker)` 상속 적용
- [ ] `KisMockApi(BaseBroker)` 상속 적용
- [ ] 기존 동작 확인 (한국 실전 테스트)

---

### STEP 2. KIS 해외주식 API 래퍼 작성 (2~4시간)

**목적**: `is_mock=True` 슬롯에서 사용할 미국주식 API

- [ ] `kis_brokers/kis_usa_api.py` 생성
- [ ] `KisUsaApi(BaseBroker)` 구현
- [ ] KIS 해외주식 TR ID 매핑

| 기능 | 한국 TR ID | 미국 TR ID |
|---|---|---|
| 잔고조회 | `VTTC8434R` | `TTTS3012R` |
| 시장가 매수 | `VTTC0802U` | `TTTS0308U` |
| 시장가 매도 | `VTTC0801U` | `TTTS0307U` |
| 현재가 | `FHKST01010100` | `HHDFS00000300` |
| 일봉 | `FHKST01010100` | `HHDFS76240000` |
| 분봉 | `FHKST03010100` | `HHDFS76950200` |

- [ ] 환율 처리: `get_account_balance()` 반환값 USD → KRW 환산 (또는 USD 그대로 내부 처리)
- [ ] `get_approval_key()` 해외 웹소켓용

---

### STEP 3. 미국장 WebSocket 작성 (1~2시간)

**목적**: 미국주식 실시간 체결가 수신

- [ ] `kis_brokers/kis_usa_websocket.py` 생성
- [ ] URL: `ws://ops.koreainvestment.com:21000` (실전 동일, TR ID만 다름)
- [ ] TR ID: `HDFSCNT0` (해외주식 실시간 체결가)
- [ ] 장시간 체크 교체: KST 기준 `23:30 ~ 06:00` (서머타임 시 `22:30 ~ 05:00`)

```python
# _is_market_hours() 교체 내용
def _is_us_market_hours() -> bool:
    now = datetime.now(KST)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    # 서머타임(3~11월): 22:30~05:00 / 표준시(11~3월): 23:30~06:00
    # 간단하게 22:00~06:30 으로 넉넉히 잡아도 됨
    return (t >= 22 * 60) or (t <= 6 * 60 + 30)
```

---

### STEP 4. 데이터 소스 교체 (4~8시간, 가장 큰 작업)

**목적**: pykrx 제거, 미국주식 데이터 소스 연결

#### 4-1. OHLCV 데이터
- [ ] `yfinance` 설치: `pip install yfinance`
- [ ] `stock_screener.py` — `pykrx.get_market_ohlcv_by_date()` → `yfinance.download()`
- [ ] `strategy.py` — `pykrx` import 제거
- [ ] `full_market_backtest.py` — yfinance 기반으로 교체
- [ ] KIS 해외 일봉 API (`HHDFS76240000`)로도 가능하나 100개 제한 있음 → yfinance 추천

#### 4-2. 종목명 조회
- [ ] `pykrx.get_market_ticker_name()` → yfinance `Ticker(t).info['shortName']`
- [ ] 또는 딕셔너리 캐시로 처리 (S&P500 등 정해진 종목 풀)

#### 4-3. 스크리너 교체
- [ ] `stock_screener.py` 미국 버전 작성 (또는 `stock_screener_us.py` 별도)
- [ ] 종목 풀: S&P500, NASDAQ100 등 (`yfinance` 또는 하드코딩)
- [ ] 섹터 분류: GICS 기준으로 교체

#### 4-4. 모멘텀 스캐너 교체
- [ ] `hot_momentum_scanner.py` — KRX 거래대금 → 미국 거래량/등락률 기준
- [ ] `yfinance`로 당일 등락률 상위 종목 스캔
- [ ] 또는 `Finviz` 스크래핑 (무료)

---

### STEP 5. 시장 국면(Regime) 기준 교체 (1~2시간)

**목적**: KODEX200 → S&P500 기반 국면 판단

- [ ] `strategy.py` — `get_market_regime()` 기준 교체
  - 현재: KODEX200 (069500) RSI 기반
  - 교체: SPY 또는 QQQ RSI 기반
- [ ] BULL/NEUTRAL/BEAR 임계값 재조정 (미국장 변동성 다름)

---

### STEP 6. bot_manager / base_bot 연결 교체 (1~2시간)

**목적**: `is_mock=True` 슬롯에 미국 API 연결

- [ ] `bots/bot_manager.py` — `is_mock=True` 일 때 `KisUsaApi` 생성하도록 변경

```python
# bot_manager.py 변경 부분
if is_mock:
    # 기존: KisMockApi(config)
    # 변경: KisUsaApi(config)
    kis = KisUsaApi(config)
    ws  = KisUsaWebSocket(approval_key, callback)
else:
    kis = KisRealApi(config)
    ws  = KisRealWebSocket(approval_key, callback)
```

- [ ] `base_bot.py` — 모의 전용 버그 대응 코드 정리 (아래 참고)

---

### STEP 7. 모의투자 전용 버그 대응 코드 정리 (1시간)

**목적**: T+2 랙 대응 코드 제거로 코드 단순화

미국 실전에서는 필요 없는 코드들:

- [ ] `core._bought_val` 관련 로직 제거 (T+2 랙 대응이었음)
- [ ] `_sync_internal_balances` — T+2 랙 주석 및 방어 코드 정리
- [ ] `deposit_delta` 감지 — `if not self._is_mock:` 조건 재검토
- [ ] WebSocket `_is_us_market_hours()` 로 교체

---

### STEP 8. UI 수정 (1~2시간)

**목적**: 미국장 슬롯 표시 조정

- [ ] `templates/index.html` — `is_mock=True` 슬롯 레이블 "모의투자" → "미국장"
- [ ] 통화 단위: KRW → USD (또는 KRW 환산 표시 선택)
- [ ] 장시간 표시: KST 기준 미국 장시간 안내
- [ ] 계좌 설정: 미국 KIS API 키 입력 폼 (appkey/appsecret/account 별도)

---

### STEP 9. 뉴스 모니터 교체 (선택, 1~2시간)

**목적**: 한국 뉴스 → 미국 뉴스

- [ ] `news_monitor.py` — 네이버/다음 뉴스 → NewsAPI.org 또는 Benzinga
- [ ] 영문 뉴스 → Claude AI 번역 후 판단 (기존 `claude_api.py` 재활용)

---

### STEP 10. 백테스트 검증 (2~4시간)

**목적**: 전략이 미국장에서도 유효한지 확인

- [ ] `full_market_backtest.py` — 미국 종목으로 백테스트 실행
- [ ] BULL/NEUTRAL/BEAR 국면별 수익률 확인
- [ ] 손절/익절 비율 재조정 (미국장 변동성 반영)

---

## 전체 예상 작업 시간

| 단계 | 예상 시간 |
|---|---|
| STEP 1. 브로커 인터페이스 | 1~2시간 |
| STEP 2. KIS 해외 API | 2~4시간 |
| STEP 3. 미국장 WebSocket | 1~2시간 |
| STEP 4. 데이터 소스 교체 | 4~8시간 (최대 작업) |
| STEP 5. 시장 국면 교체 | 1~2시간 |
| STEP 6. bot_manager 연결 | 1~2시간 |
| STEP 7. 레거시 코드 정리 | 1시간 |
| STEP 8. UI 수정 | 1~2시간 |
| STEP 9. 뉴스 모니터 | 1~2시간 (선택) |
| STEP 10. 백테스트 검증 | 2~4시간 |
| **합계** | **15~29시간** |

---

## 주요 참고 사항

### KIS 해외주식 API 문서
- KIS Developers: https://apiportal.koreainvestment.com
- 해외주식 TR ID 목록: 포털 > API 문서 > 해외주식

### 미국장 특이사항
- 서머타임: 3월 둘째 일요일 ~ 11월 첫째 일요일 (1시간 빠름)
- 결제: T+2 (한국과 동일)
- 실전 모의 없음 → 소액으로 실전 테스트 필요
- 소수점 매수 불가 (KIS 기준, 1주 단위)

### pykrx 완전 제거 체크리스트
- [ ] `stock_screener.py`
- [ ] `strategy.py`
- [ ] `hot_momentum_scanner.py`
- [ ] `app.py` (종목명 조회 부분)
- [ ] `backtest.py`
- [ ] `core_satellite_backtest.py`
- [ ] `full_market_backtest.py`
- [ ] `requirements.txt` 에서 `pykrx` 제거

### 토스 API 전환 시 (미래)
- `kis_brokers/base_broker.py` 의 9개 메서드만 구현한 `TossApi` 클래스 추가
- `bot_manager.py` 에서 증권사 선택 로직 추가
- `base_bot.py` 변경 불필요
