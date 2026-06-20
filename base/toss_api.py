"""
base/toss_api.py — 토스증권 Open API 통합 래퍼
────────────────────────────────────────────────
https://developers.tossinvest.com/docs

- OAuth2 토큰 관리 (24h 캐시, 재발급 시 이전 무효화 대응)
- 시장 데이터: 현재가(200종목 일괄), 캔들, 상하한가, 환율, 장운영정보
- 계좌/자산: 보유주식, 매수가능금액
- 주문: KR 지정가(시장가 불가), US 시장가/지정가
- 토스증권 compat 메서드: get_account_balance(), get_balance(), buy_market_order(),
                  sell_market_order(), get_ohlcv(), get_prices_batch() 등
"""

from __future__ import annotations
import time
import threading
import logging
import requests
import json
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger("lassi_bot")

_BASE   = "https://openapi.tossinvest.com"
_KST    = ZoneInfo("Asia/Seoul")
_ET     = ZoneInfo("America/New_York")


# ── 호가 단위 반올림 (KRX 규정) ──────────────────────────────────────────
def _round_to_tick(price: float) -> int:
    p = int(price)
    if p < 1_000:    return round(p / 1) * 1
    if p < 5_000:    return round(p / 5) * 5
    if p < 10_000:   return round(p / 10) * 10
    if p < 50_000:   return round(p / 50) * 50
    if p < 100_000:  return round(p / 100) * 100
    if p < 500_000:  return round(p / 500) * 500
    return round(p / 1_000) * 1_000


def _is_kr_symbol(symbol: str) -> bool:
    """KR 종목 여부: 6자리 숫자 코드"""
    return symbol.isdigit() and len(symbol) == 6


class TossInvestApi:
    """토스증권 Open API 래퍼 — KR/US 통합 단일 계좌"""

    def __init__(self, client_id: str, client_secret: str, account_seq: str = ""):
        self.client_id     = client_id.strip()
        self.client_secret = client_secret.strip()
        self._account_seq_raw = account_seq.strip()
        self.account_seq   = ""                    # 실제 사용할 정수 seq (자동 조회)
        self._token        = ""
        self._token_exp    = 0.0
        self._lock         = threading.Lock()
        # accountSeq 자동 조회 (사용자 입력값은 무시 — API에서 정수로 받아와야 함)
        self._init_account_seq()
        logger.info(f"[Toss] API 초기화 완료 (계좌seq: {self.account_seq or '미설정'})")

    def _init_account_seq(self):
        """GET /api/v1/accounts 로 실제 accountSeq(정수) 자동 조회."""
        try:
            accounts = self.get_accounts()
            if accounts:
                seq = accounts[0].get("accountSeq")
                if seq is not None:
                    self.account_seq = str(int(seq))
                    logger.info(f"[Toss] 계좌 자동 조회 완료: accountSeq={self.account_seq} "
                                f"(accountNo={accounts[0].get('accountNo', '?')})")
                    return
            logger.warning("[Toss] 계좌 목록이 비어있습니다.")
        except Exception as e:
            logger.warning(f"[Toss] 계좌 자동 조회 실패: {e}")

    # ── 인증 ────────────────────────────────────────────────────────────
    def _get_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
            try:
                res = requests.post(
                    f"{_BASE}/oauth2/token",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data={
                        "grant_type":    "client_credentials",
                        "client_id":     self.client_id,
                        "client_secret": self.client_secret,
                    },
                    timeout=10,
                )
                data = res.json()
                token = data.get("access_token", "")
                if not token:
                    err = (data.get("error") or {})
                    code = err.get("code", "") if isinstance(err, dict) else str(err)
                    logger.error(f"[Toss] 토큰 발급 실패: {data}")
                    # rate-limit / IP 차단 → 락 해제 후 60초 대기 (lock 밖에서 sleep)
                    if "rate" in str(code).lower() or "access_denied" in str(data):
                        self._token_exp = time.time() + 60  # 60초 후 재시도 허용
                    return self._token or ""
                self._token     = token
                self._token_exp = time.time() + int(data.get("expires_in", 86400)) - 300
                logger.info("[Toss] 토큰 발급 완료")
                return token
            except Exception as e:
                logger.error(f"[Toss] 토큰 발급 오류: {e}")
                return self._token or ""

    def _h(self, with_account: bool = False) -> dict:
        h: dict = {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type":  "application/json",
        }
        if with_account and self.account_seq:
            h["X-Tossinvest-Account"] = self.account_seq
        return h

    # ── 공통 HTTP 헬퍼 ──────────────────────────────────────────────────
    def _get(self, path: str, params: dict | None = None, with_account: bool = False):
        try:
            r = requests.get(
                f"{_BASE}{path}",
                headers=self._h(with_account),
                params=params,
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                # 토스 API는 모든 응답을 {"result": ...} 로 래핑
                return data.get("result", data) if isinstance(data, dict) and "result" in data else data
            logger.warning(f"[Toss] GET {path} → {r.status_code}: {r.text[:300]}")
        except Exception as e:
            logger.error(f"[Toss] GET {path} 오류: {e}")
        return None

    def _post(self, path: str, body: dict, with_account: bool = False):
        try:
            r = requests.post(
                f"{_BASE}{path}",
                headers=self._h(with_account),
                json=body,
                timeout=10,
            )
            if r.status_code in (200, 201):
                data = r.json()
                return data.get("result", data) if isinstance(data, dict) and "result" in data else data
            logger.warning(f"[Toss] POST {path} → {r.status_code}: {r.text[:300]}")
        except Exception as e:
            logger.error(f"[Toss] POST {path} 오류: {e}")
        return None

    # ────────────────────────────────────────────────────────────────────
    # 현재가
    # ────────────────────────────────────────────────────────────────────
    def get_prices(self, symbols: list[str]) -> dict[str, float]:
        """최대 200종목 현재가 일괄 조회. {symbol: float} 반환."""
        if not symbols:
            return {}
        result: dict[str, float] = {}
        for i in range(0, len(symbols), 200):
            chunk = symbols[i : i + 200]
            data  = self._get("/api/v1/prices", {"symbols": ",".join(chunk)})
            if data and isinstance(data, list):
                for item in data:
                    sym = item.get("symbol", "")
                    p   = item.get("lastPrice")
                    if sym and p is not None:
                        result[sym] = float(p)
        return result

    def get_price(self, symbol: str) -> float:
        return self.get_prices([symbol]).get(symbol, 0.0)

    def get_current_price(self, symbol: str) -> float | None:
        """토스증권 compat"""
        p = self.get_price(symbol)
        return p if p > 0 else None

    def get_prices_batch(self, symbols: list[str]) -> dict[str, float]:
        """토스증권 compat"""
        return self.get_prices(symbols)

    # ────────────────────────────────────────────────────────────────────
    # 캔들 / OHLCV
    # ────────────────────────────────────────────────────────────────────
    def get_candles(
        self,
        symbol:   str,
        interval: str = "1d",
        count:    int = 100,
        adjusted: bool = True,
    ) -> pd.DataFrame:
        """OHLCV 캔들. interval: '1m' | '1d'. count 기준 최근 봉 반환."""
        all_candles: list = []
        before: str | None = None
        remaining = count
        while remaining > 0:
            params: dict = {
                "symbol":   symbol,
                "interval": interval,
                "count":    min(remaining, 200),
                "adjusted": str(adjusted).lower(),
            }
            if before:
                params["before"] = before
            data = self._get("/api/v1/candles", params)
            if not data:
                break
            candles = data.get("candles", [])
            if not candles:
                break
            all_candles.extend(candles)
            remaining -= len(candles)
            before = data.get("nextBefore")
            if not before:
                break

        if not all_candles:
            return pd.DataFrame()

        df = pd.DataFrame(all_candles)
        if "timestamp" in df.columns:
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
            df = df.sort_values("datetime").reset_index(drop=True)

        col_map = {
            "openPrice":  "open",
            "highPrice":  "high",
            "lowPrice":   "low",
            "closePrice": "close",
            "volume":     "volume",
        }
        df = df.rename(columns=col_map)
        for c in ("open", "high", "low", "close", "volume"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df

    def get_full_history(self, symbol: str) -> pd.DataFrame:
        all_candles: list = []
        before: str | None = None
        while True:
            params: dict = {
                "symbol":   symbol,
                "interval": "1d",
                "count":    200,
                "adjusted": "true",
            }
            if before:
                params["before"] = before
            data = self._get("/api/v1/candles", params)
            if not data:
                break
            candles = data.get("candles", [])
            if not candles:
                break
            all_candles.extend(candles)
            before = data.get("nextBefore")
            if not before:
                break
            time.sleep(0.1)

        if not all_candles:
            return pd.DataFrame()

        df = pd.DataFrame(all_candles)
        if "timestamp" in df.columns:
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
            df = df.set_index("datetime").sort_index()

        col_map = {
            "openPrice":  "open",
            "highPrice":  "high",
            "lowPrice":   "low",
            "closePrice": "close",
            "volume":     "volume",
        }
        df = df.rename(columns=col_map)
        for c in ("open", "high", "low", "close", "volume"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])

    def get_ohlcv(self, symbol: str, period: str = "D") -> pd.DataFrame:
        interval = "1m" if period == "M" else "1d"
        return self.get_candles(symbol, interval=interval, count=200)

    def get_minute_candles(self, ticker: str, count: int = 10, market: str = "J") -> pd.DataFrame:
        """토스증권 compat"""
        return self.get_candles(ticker, interval="1m", count=count)

    def get_realtime_price_data(self, symbol: str) -> dict | None:
        """토스증권 compat — 현재가 dict 형태로 반환"""
        p = self.get_price(symbol)
        if p > 0:
            return {"stck_prpr": str(int(p)), "prpr": str(int(p))}
        return None

    # ────────────────────────────────────────────────────────────────────
    # 상하한가
    # ────────────────────────────────────────────────────────────────────
    def get_price_limits(self, symbol: str) -> dict:
        """{'upper': int, 'lower': int}"""
        data = self._get("/api/v1/price-limits", {"symbol": symbol})
        if data:
            return {
                "upper": int(float(data.get("upperLimitPrice", 0))),
                "lower": int(float(data.get("lowerLimitPrice", 0))),
            }
        return {"upper": 0, "lower": 0}

    # ────────────────────────────────────────────────────────────────────
    # 환율
    # ────────────────────────────────────────────────────────────────────
    def get_exchange_rate(self, base: str = "USD", quote: str = "KRW") -> float:
        """실시간 환율 (1분 갱신). 실패 시 0.0 반환."""
        data = self._get(
            "/api/v1/exchange-rate",
            {"baseCurrency": base, "quoteCurrency": quote},
        )
        if data:
            return float(data.get("rate") or data.get("midRate") or 0)
        return 0.0

    # ────────────────────────────────────────────────────────────────────
    # 장 운영 시간
    # ────────────────────────────────────────────────────────────────────
    def get_market_calendar_kr(self) -> dict:
        return self._get("/api/v1/market-calendar/KR") or {}

    def get_market_calendar_us(self) -> dict:
        return self._get("/api/v1/market-calendar/US") or {}

    @staticmethod
    def _parse_iso(s: str):
        """ISO 8601 문자열 → timezone-aware datetime. 파싱 실패 시 None."""
        if not s:
            return None
        try:
            # Python 3.11+ fromisoformat 지원, 하위 버전 대응
            return datetime.fromisoformat(s)
        except Exception:
            return None

    def is_kr_market_open(self) -> bool:
        """KRX 정규장 여부 (API 기반). 실패 시 시간 기반 fallback."""
        try:
            cal     = self.get_market_calendar_kr()
            regular = (cal.get("today") or {}).get("regularMarket") or {}
            start   = self._parse_iso(regular.get("startTime", ""))
            end     = self._parse_iso(regular.get("endTime",   ""))
            if start and end:
                now = datetime.now(start.tzinfo)
                return start <= now < end
        except Exception:
            pass
        # Fallback: KST 09:00~15:30 평일
        now = datetime.now(_KST)
        if now.weekday() >= 5:
            return False
        return now.replace(hour=9, minute=0, second=0, microsecond=0) <= now < now.replace(hour=15, minute=30, second=0, microsecond=0)

    def is_us_market_open(self) -> bool:
        """US 정규장 여부 (API 기반). 실패 시 시간 기반 fallback."""
        try:
            cal     = self.get_market_calendar_us()
            regular = (cal.get("today") or {}).get("regularMarket") or {}
            start   = self._parse_iso(regular.get("startTime", ""))
            end     = self._parse_iso(regular.get("endTime",   ""))
            if start and end:
                now = datetime.now(start.tzinfo)
                return start <= now < end
        except Exception:
            pass
        # Fallback: ET 09:30~16:00 평일
        now = datetime.now(_ET)
        if now.weekday() >= 5:
            return False
        t_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
        t_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
        return t_open <= now < t_close

    # ────────────────────────────────────────────────────────────────────
    # 종목 정보
    # ────────────────────────────────────────────────────────────────────
    def get_stock_info(self, symbols: list[str]) -> list[dict]:
        if not symbols:
            return []
        result = []
        for i in range(0, len(symbols), 200):
            chunk = symbols[i : i + 200]
            data  = self._get("/api/v1/stocks", {"symbols": ",".join(chunk)})
            if data and isinstance(data, list):
                result.extend(data)
        return result

    def get_warnings(self, symbol: str) -> list[dict]:
        data = self._get(f"/api/v1/stocks/{symbol}/warnings")
        return data if isinstance(data, list) else []

    def has_investment_warning(self, symbol: str) -> bool:
        """투자경고·VI 발동 여부 — 매수 전 체크용"""
        warnings = self.get_warnings(symbol)
        danger = {"INVESTMENT_WARNING", "OVERHEATED", "LIQUIDATION_TRADING"}
        return any(w.get("warningType") in danger for w in warnings)

    def search_stock_name(self, query: str) -> list[dict]:
        """토스증권 compat — symbol 직접 조회로 대체"""
        data = self.get_stock_info([query.upper()])
        if data:
            return [{"ticker": d["symbol"], "name": d.get("name", d["symbol"])} for d in data]
        return []

    def get_etf_price(self, etf_code: str) -> dict | None:
        """토스증권 compat"""
        price = self.get_price(etf_code)
        return {"current_price": price} if price > 0 else None

    # ────────────────────────────────────────────────────────────────────
    # 계좌
    # ────────────────────────────────────────────────────────────────────
    def get_accounts(self) -> list[dict]:
        data = self._get("/api/v1/accounts")
        return data if isinstance(data, list) else []

    # ────────────────────────────────────────────────────────────────────
    # 보유 자산
    # ────────────────────────────────────────────────────────────────────
    def get_holdings_raw(self, symbol: str | None = None) -> dict:
        params: dict = {}
        if symbol:
            params["symbol"] = symbol
        raw = self._get("/api/v1/holdings", params, with_account=True) or {}
        # 응답 구조 진단 로그 (첫 호출 시 또는 items 없을 때)
        if not getattr(self, '_holdings_logged', False) or not raw.get("items"):
            import json as _json
            _sample = {k: (v[:2] if isinstance(v, list) else v) for k, v in raw.items()}
            logger.info(f"[Toss] holdings 응답 구조: {_json.dumps(_sample, ensure_ascii=False, default=str)[:500]}")
            self._holdings_logged = True
        return raw

    def _build_name_map(self, symbols: list[str]) -> dict[str, str]:
        info_list = self.get_stock_info(symbols)
        return {i["symbol"]: i.get("name", i["symbol"]) for i in info_list}

    @staticmethod
    def _krw(obj) -> float:
        """{"krw": "...", "usd": "..."} 또는 숫자/문자열에서 KRW 값 추출."""
        if isinstance(obj, dict):
            return float(obj.get("krw") or 0)
        try:
            return float(obj or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _usd(obj) -> float:
        """{"krw": "...", "usd": "..."} 또는 숫자/문자열에서 USD 값 추출."""
        if isinstance(obj, dict):
            return float(obj.get("usd") or 0)
        try:
            return float(obj or 0)
        except (TypeError, ValueError):
            return 0.0

    def get_account_balance(self) -> dict:
        """토스증권 KR 잔고 조회:
        {cash, stocks:[{ticker,name,shares,purchase_price,current_price,profit_loss}], total_value}
        KR 종목(6자리 숫자)만 반환.
        """
        raw = self.get_holdings_raw()
        if not raw:
            return {}

        items    = raw.get("items", [])
        kr_items = [i for i in items if _is_kr_symbol(str(i.get("symbol", "")))]

        symbols  = [i["symbol"] for i in kr_items]
        name_map = self._build_name_map(symbols) if symbols else {}

        stocks = []
        for item in kr_items:
            sym   = item.get("symbol", "")
            qty   = int(float(item.get("quantity", 0)))
            avg_p = float(item.get("averagePurchasePrice", 0) or 0)
            cur_p = float(item.get("lastPrice", 0) or 0)
            pl    = item.get("profitLoss") or {}
            # profitLoss.amount = {"krw": "...", "usd": "..."}
            profit = self._krw(pl.get("amount"))
            profit_rt = float(pl.get("rate") or 0) * 100  # 소수 → %
            stocks.append({
                "ticker":         sym,
                "name":           name_map.get(sym, sym),
                "shares":         qty,
                "purchase_price": avg_p,
                "current_price":  cur_p,
                "profit_loss":    profit,
                "profit_rt":      profit_rt,
            })

        # marketValue.amount = {"krw": "...", "usd": "..."}
        mv_obj   = raw.get("marketValue") or {}
        total_mv = self._krw((mv_obj.get("amount") if isinstance(mv_obj, dict) else mv_obj))

        # 매수가능금액 — API 실패 시 0으로 표시 (잔고는 보임)
        cash_krw = self.get_buyable_cash()

        return {
            "cash":        cash_krw,
            "stocks":      stocks,
            "total_value": total_mv + cash_krw,
            "total_cash":  cash_krw,
        }

    def get_balance(self) -> dict:
        """토스증권 US 잔고 조회:
        {cash_usd, stocks:[{ticker,name,shares,avg_price,current_price,value}]}
        US 종목(알파벳 티커)만 반환.
        """
        raw = self.get_holdings_raw()
        if not raw:
            return {"cash_usd": 0.0, "stocks": []}

        items    = raw.get("items", [])
        us_items = [i for i in items if not _is_kr_symbol(str(i.get("symbol", "")))]

        stocks = []
        for item in us_items:
            sym   = item.get("symbol", "")
            qty   = float(item.get("quantity", 0) or 0)
            avg_p = float(item.get("averagePurchasePrice", 0) or 0)
            cur_p = float(item.get("lastPrice", 0) or 0)
            # marketValue.amount = {"krw": "...", "usd": "..."}
            mv_amount = (item.get("marketValue") or {}).get("amount")
            value = self._usd(mv_amount) if mv_amount else qty * cur_p
            pl    = item.get("profitLoss") or {}
            profit_rt = float(pl.get("rate") or 0) * 100
            stocks.append({
                "ticker":        sym,
                "name":          sym,
                "shares":        qty,
                "avg_price":     avg_p,
                "current_price": cur_p,
                "value":         value,
                "profit_rt":     profit_rt,
            })

        cash_usd = self.get_buyable_cash_usd()
        return {"cash_usd": cash_usd, "stocks": stocks}

    # ────────────────────────────────────────────────────────────────────
    # 매수가능금액 / 매도가능수량
    # ────────────────────────────────────────────────────────────────────
    def _extract_cash(self, data: dict | None, currency: str) -> float:
        """buying-power 응답에서 금액 추출. 응답 구조 불확실하여 가능한 모든 필드 시도."""
        if not data:
            return 0.0
        # 가능한 필드명들 시도
        for key in ("amount", "buyingPower", "buyableAmount", "availableAmount", "cash"):
            val = data.get(key)
            if val is not None:
                if isinstance(val, dict):
                    return float(val.get(currency) or 0)
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
        # 최상위가 직접 {"krw": ..., "usd": ...} 구조인 경우
        if currency in data:
            try:
                return float(data[currency] or 0)
            except (TypeError, ValueError):
                pass
        logger.warning(f"[Toss] buying-power 알 수 없는 응답 구조: {data}")
        return 0.0

    def get_buyable_cash(self, stock_code: str = "005930", price: int = 0) -> float:
        """KRW 매수가능금액"""
        data = self._get("/api/v1/buying-power", {"currency": "KRW"}, with_account=True)
        if data:
            val = data.get("cashBuyingPower")
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
        return 0.0

    def get_buyable_cash_usd(self, ticker: str = "AAPL", price: float = 0) -> float:
        """USD 매수가능금액"""
        data = self._get("/api/v1/buying-power", {"currency": "USD"}, with_account=True)
        if data:
            val = data.get("cashBuyingPower")
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
        return 0.0

    def get_sellable_qty(self, symbol: str) -> int:
        """매도가능수량"""
        data = self._get("/api/v1/sellable-quantity", {"symbol": symbol}, with_account=True)
        if data:
            logger.info(f"[Toss] sellable-quantity 응답 구조: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            for key in ("quantity", "sellableQuantity", "sellable", "availableQuantity"):
                val = data.get(key)
                if val is not None:
                    try:
                        return int(float(val))
                    except (TypeError, ValueError):
                        pass
        return 0

    # ────────────────────────────────────────────────────────────────────
    # 주문
    # ────────────────────────────────────────────────────────────────────
    def _place_order(
        self,
        symbol:       str,
        side:         str,         # "BUY" | "SELL"
        order_type:   str,         # "LIMIT" | "MARKET"
        qty:          float | None = None,
        price:        float | None = None,
        order_amount: float | None = None,
        time_in_force: str = "DAY",
    ) -> bool:
        body: dict = {
            "symbol":      symbol,
            "side":        side,
            "orderType":   order_type,
            "timeInForce": time_in_force,
        }
        if qty is not None:
            # 정수 수량은 문자열 정수로 전송
            body["quantity"] = str(int(qty)) if float(qty) == int(float(qty)) else str(qty)
        if price is not None and order_type == "LIMIT":
            body["price"] = str(int(price))
        if order_amount is not None:
            body["orderAmount"] = str(order_amount)

        data = self._post("/api/v1/orders", body, with_account=True)
        if data and data.get("orderId"):
            logger.info(f"[Toss] {side} {symbol} {order_type} 주문 완료: {data['orderId']}")
            return True
        return False

    def buy_market_order(self, symbol: str, qty: int, price: int = 0) -> bool:
        """토스증권 compat — KR: 지정가(시장가 불가), US: 시장가
        price=0  → KR: 상한가(즉시체결), US: 시장가
        price>0  → KR/US: 지정가
        price=-1 → 강제 시장가(US only)
        """
        is_kr = _is_kr_symbol(symbol)
        if is_kr:
            # ── KR 지정가 매수 ──────────────────────────────────
            if price == -1 or price == 0:
                # 상한가로 즉시 체결 유도
                limits = self.get_price_limits(symbol)
                upper  = limits.get("upper", 0)
                if upper <= 0:
                    cur   = self.get_price(symbol)
                    upper = int(cur * 1.05) if cur > 0 else 0
                price = upper
            if price <= 0:
                logger.warning(f"[Toss] KR 매수 가격 조회 실패: {symbol}")
                return False
            price = _round_to_tick(price)
            return self._place_order(symbol, "BUY", "LIMIT", qty=qty, price=price)
        else:
            # ── US 시장가 매수 ──────────────────────────────────
            if qty and qty > 0:
                return self._place_order(symbol, "BUY", "MARKET", qty=qty)
            return False

    def sell_market_order(self, symbol: str, qty: int, price: int = 0) -> bool:
        """토스증권 compat — KR: 지정가, US: 시장가
        price=0  → KR: 현재가-1틱(빠른체결), US: 시장가
        price>0  → 지정가
        """
        is_kr = _is_kr_symbol(symbol)
        if is_kr:
            # ── KR 지정가 매도 ──────────────────────────────────
            if price <= 0:
                cur   = self.get_price(symbol)
                price = int(cur * 0.99) if cur > 0 else 0
            if price <= 0:
                limits = self.get_price_limits(symbol)
                price  = limits.get("lower", 0)
            if price <= 0:
                logger.warning(f"[Toss] KR 매도 가격 조회 실패: {symbol}")
                return False
            price = _round_to_tick(price)
            return self._place_order(symbol, "SELL", "LIMIT", qty=qty, price=price)
        else:
            # ── US 시장가 매도 ──────────────────────────────────
            return self._place_order(symbol, "SELL", "MARKET", qty=int(qty))

    def buy_market_us(self, symbol: str, qty: int) -> bool:
        return self._place_order(symbol, "BUY", "MARKET", qty=qty)

    def sell_market_us(self, symbol: str, qty: int) -> bool:
        return self._place_order(symbol, "SELL", "MARKET", qty=qty)

    def buy_fractional_order(self, symbol: str, amount_usd: float) -> bool:
        """US 소수점 매수 (금액 기반, 정규장 전용)"""
        return self._place_order(symbol, "BUY", "MARKET", order_amount=amount_usd)

    def sell_fractional_order(self, symbol: str, qty: float) -> bool:
        """US 소수점 매도 — 정수 처리(Toss 소수점 수량 지원 미확인)"""
        qty_int = max(1, int(qty))
        return self._place_order(symbol, "SELL", "MARKET", qty=qty_int)

    # ────────────────────────────────────────────────────────────────────
    # 주문 조회 / 취소
    # ────────────────────────────────────────────────────────────────────
    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        params: dict = {"status": "OPEN"}
        if symbol:
            params["symbol"] = symbol
        data = self._get("/api/v1/orders", params, with_account=True)
        return (data or {}).get("orders", [])

    def get_unfilled_orders(self) -> list[dict]:
        """토스증권 compat"""
        return self.get_open_orders()

    def get_filled_orders(
        self,
        symbol:    str | None = None,
        from_date: str | None = None,
        to_date:   str | None = None,
    ) -> list[dict]:
        params: dict = {"status": "CLOSED", "limit": 100}
        if symbol:
            params["symbol"] = symbol
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        data = self._get("/api/v1/orders", params, with_account=True)
        return (data or {}).get("orders", [])

    def get_order_fills(self, date_str: str = "") -> list[dict]:
        """토스증권 compat"""
        today = datetime.now(_KST).strftime("%Y-%m-%d")
        return self.get_filled_orders(from_date=date_str or today, to_date=today)

    def cancel_order(self, order_id: str, **_kwargs) -> bool:
        data = self._post(f"/api/v1/orders/{order_id}/cancel", {}, with_account=True)
        return data is not None

    def cancel_all_unfilled(self) -> int:
        """미체결 주문 전량 취소. 취소 건수 반환."""
        orders  = self.get_open_orders()
        success = 0
        for o in orders:
            oid = o.get("orderId", "")
            if oid and self.cancel_order(oid):
                success += 1
        return success

    # ────────────────────────────────────────────────────────────────────
    # 토스증권 미지원 메서드 stubs (스크리너 호환 — 데이터 없음 → 빈 결과)
    # ────────────────────────────────────────────────────────────────────
    def get_volume_rank(self, market_div: str = "J", limit: int = 30) -> list:
        """토스증권 미지원 — 빈 리스트 반환 (스크리너 fallback 처리)"""
        return []

    def get_price_change_rank(self, market_div: str = "J", limit: int = 20) -> list:
        """토스증권 미지원"""
        return []

    def get_foreign_institution_rank(self, market_div: str = "0000", limit: int = 30) -> list:
        """토스증권 미지원"""
        return []

    def get_foreign_buy_rank(self, market_div: str = "J", limit: int = 30) -> list:
        """토스증권 미지원"""
        return []

