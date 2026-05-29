"""
kis_brokers/kis_overseas_api.py — KIS 해외주식 실전 매매 API
─────────────────────────────────────────────────────────────
한국투자증권 OpenAPI 해외주식 전용 래퍼.
- 토큰 발급 / 자동 갱신 (23시간 캐시)
- 미국주식 현재가 조회 (NASDAQ / NYSE 자동 폴백)
- 시장가 매수 / 매도 주문
- 해외주식 잔고 조회 (USD)
"""

import time
import json
import logging
import threading
import requests
from datetime import datetime, timedelta

logger = logging.getLogger('lassi_bot')

_BASE_URL = "https://openapi.koreainvestment.com:9443"

# 미국 거래소 코드 (KIS 규격)
EXCHANGE_NAS  = "NASD"   # NASDAQ
EXCHANGE_NYSE = "NYSE"   # NYSE
EXCHANGE_AMEX = "AMEX"   # AMEX


class KisOverseasApi:
    """KIS 해외주식 실전 매매 API"""

    def __init__(self, app_key: str, app_secret: str, account_no: str):
        self.app_key    = app_key.strip()
        self.app_secret = app_secret.strip()
        # 계좌번호 파싱: "12345678-01" or "1234567801" → cano(8) + acnt_cd(2)
        raw = (account_no or '').replace('-', '').strip()
        self.cano    = raw[:8]
        self.acnt_cd = raw[8:10] if len(raw) >= 10 else '01'

        self._token     : str   = ''
        self._token_exp : float = 0.0
        self._lock = threading.Lock()

        logger.info(f"[KIS해외] API 초기화 완료 (계좌: {self.cano}-{self.acnt_cd})")

    # ── 토큰 ──────────────────────────────────────────────────────────

    def _get_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
        try:
            res = requests.post(
                f"{_BASE_URL}/oauth2/tokenP",
                headers={"content-type": "application/json"},
                data=json.dumps({
                    "grant_type": "client_credentials",
                    "appkey":     self.app_key,
                    "appsecret":  self.app_secret,
                }),
                timeout=10,
            )
            data  = res.json()
            token = data.get("access_token", "")
            exp   = time.time() + int(data.get("expires_in", 86400)) - 300
            with self._lock:
                self._token     = token
                self._token_exp = exp
            logger.info("[KIS해외] 토큰 발급 완료")
            return token
        except Exception as e:
            logger.error(f"[KIS해외] 토큰 발급 실패: {e}")
            return ""

    def _headers(self, tr_id: str) -> dict:
        return {
            "content-type":  "application/json; charset=utf-8",
            "authorization": f"Bearer {self._get_token()}",
            "appkey":        self.app_key,
            "appsecret":     self.app_secret,
            "tr_id":         tr_id,
            "custtype":      "P",
        }

    # ── 현재가 조회 ───────────────────────────────────────────────────

    def get_price(self, ticker: str, exchange: str = EXCHANGE_NAS) -> float:
        """해외주식 현재가 조회 (USD). NASDAQ 실패 시 NYSE 재시도."""
        try:
            res = requests.get(
                f"{_BASE_URL}/uapi/overseas-price/v1/quotations/price",
                headers=self._headers("HHDFS76200200"),
                params={"AUTH": "", "EXCD": exchange, "SYMB": ticker},
                timeout=10,
            )
            data   = res.json()
            output = data.get("output", {})
            price  = float(output.get("last", 0) or 0)
            if price <= 0 and exchange == EXCHANGE_NAS:
                # NASDAQ 조회 실패 → NYSE 재시도
                return self.get_price(ticker, EXCHANGE_NYSE)
            return price
        except Exception as e:
            logger.debug(f"[KIS해외] {ticker} 현재가 조회 실패: {e}")
            return 0.0

    def get_prices_batch(self, tickers: list) -> dict[str, float]:
        """복수 종목 현재가 순차 조회 ({ticker: price_usd})"""
        prices: dict[str, float] = {}
        for t in tickers:
            p = self.get_price(t)
            if p > 0:
                prices[t] = p
            time.sleep(0.05)   # API 호출 간격
        return prices

    # ── 주문 ──────────────────────────────────────────────────────────

    def _order(self, ticker: str, qty: int, side: str,
               exchange: str = EXCHANGE_NAS) -> bool:
        """
        해외주식 시장가 주문.
        side='BUY'  → tr_id=TTTT1002U
        side='SELL' → tr_id=TTTT1006U
        """
        if qty <= 0:
            return False
        tr_id = "TTTT1002U" if side == "BUY" else "TTTT1006U"
        body  = {
            "CANO":            self.cano,
            "ACNT_PRDT_CD":    self.acnt_cd,
            "OVRS_EXCG_CD":    exchange,
            "PDNO":            ticker,
            "ORD_QTY":         str(qty),
            "OVRS_ORD_UNPR":   "0",    # 시장가: 단가 0
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN":        "00",   # 지정가→"00" (해외는 시장가도 00 사용)
        }
        try:
            res  = requests.post(
                f"{_BASE_URL}/uapi/overseas-stock/v1/trading/order",
                headers=self._headers(tr_id),
                data=json.dumps(body),
                timeout=10,
            )
            data  = res.json()
            rt_cd = data.get("rt_cd", "9")
            msg   = data.get("msg1", "")
            if rt_cd == "0":
                logger.info(f"[KIS해외] {side} {ticker} {qty}주 접수 완료: {msg}")
                return True
            else:
                logger.warning(f"[KIS해외] {side} {ticker} {qty}주 실패 (rt_cd={rt_cd}): {msg}")
                return False
        except Exception as e:
            logger.error(f"[KIS해외] 주문 통신 오류: {e}")
            return False

    def buy_market_order(self, ticker: str, qty: int,
                         exchange: str = EXCHANGE_NAS) -> bool:
        """시장가 매수 (정수 주)"""
        return self._order(ticker, qty, "BUY", exchange)

    def sell_market_order(self, ticker: str, qty: int,
                          exchange: str = EXCHANGE_NAS) -> bool:
        """시장가 매도 (정수 주)"""
        return self._order(ticker, qty, "SELL", exchange)

    def buy_fractional_order(self, ticker: str, qty_decimal: float,
                             exchange: str = EXCHANGE_NAS) -> bool:
        """
        소수단위(소수점) 매수 — 정규장(09:30~16:00 ET)에서만 사용.
        KIS 소수단위 해외주식 매수: TR_ID TTTS0307U (나스닥/NYSE)
        qty_decimal 예시: 0.5, 1.25, 2.0 (소수점 3자리까지)

        ※ 실계좌에서 KIS 소수단위 매매 서비스 신청 필수.
        """
        qty_str = f"{qty_decimal:.3f}"
        tr_id = "TTTS0307U"   # 소수단위 해외주식 매수
        body = {
            "CANO":            self.cano,
            "ACNT_PRDT_CD":    self.acnt_cd,
            "OVRS_EXCG_CD":    exchange,
            "PDNO":            ticker,
            "ORD_QTY":         qty_str,
            "OVRS_ORD_UNPR":   "0",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN":        "00",
        }
        try:
            res  = requests.post(
                f"{_BASE_URL}/uapi/overseas-stock/v1/trading/order",
                headers=self._headers(tr_id),
                data=json.dumps(body),
                timeout=10,
            )
            data  = res.json()
            rt_cd = data.get("rt_cd", "9")
            msg   = data.get("msg1", "")
            if rt_cd == "0":
                logger.info(f"[KIS해외] 소수단위 BUY {ticker} {qty_str}주 접수 완료: {msg}")
                return True
            else:
                logger.warning(f"[KIS해외] 소수단위 BUY {ticker} {qty_str}주 실패 (rt_cd={rt_cd}): {msg}")
                return False
        except Exception as e:
            logger.error(f"[KIS해외] 소수단위 주문 통신 오류: {e}")
            return False

    # ── 매수가능금액 (T+2 fix) ────────────────────────────────────────

    def get_buyable_cash_usd(self, ticker: str = "AAPL",
                              price: float = 0.0,
                              exchange: str = EXCHANGE_NAS) -> float:
        """해외주식 매수가능금액조회 (TTTS3007R) → USD 기준 실제 주문가능금액.
        매도 당일에도 T+2 정산 전 재사용 가능 금액(sll_ruse_psbl_amt) 포함.
        """
        try:
            res = requests.get(
                f"{_BASE_URL}/uapi/overseas-stock/v1/trading/inquire-psamount",
                headers=self._headers("TTTS3007R"),
                params={
                    "CANO":          self.cano,
                    "ACNT_PRDT_CD":  self.acnt_cd,
                    "OVRS_EXCG_CD":  exchange,
                    "OVRS_ORD_UNPR": str(price) if price > 0 else "0",
                    "ITEM_CD":       ticker,
                },
                timeout=10,
            )
            data   = res.json()
            output = data.get("output", {}) or {}
            # ovrs_ord_psbl_amt: 외화 기준 주문가능금액 (T+2 매도대금 포함)
            amt = float(output.get("ovrs_ord_psbl_amt", 0) or 0)
            if amt <= 0:
                amt = float(output.get("frcr_ord_psbl_amt1", 0) or 0)
            return amt
        except Exception as e:
            logger.debug(f"[KIS해외] 매수가능금액 조회 실패: {e}")
            return 0.0

    # ── 복수종목 시세 ─────────────────────────────────────────────────

    def get_prices_batch_multi(self, tickers: list,
                                exchange: str = "NAS") -> dict[str, float]:
        """복수종목 시세조회 (HHDFS76220000) — 최대 10개씩 청크.
        Returns {ticker: price_usd}
        """
        # 거래소 코드 매핑 (ranking 형식 → 주문 형식 역방향 아님, 그대로 사용)
        excd_map = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS",
                    "NAS": "NAS", "NYS": "NYS", "AMS": "AMS"}
        excd = excd_map.get(exchange, "NAS")

        prices: dict[str, float] = {}
        for i in range(0, len(tickers), 10):
            chunk = tickers[i:i + 10]
            params: dict = {"AUTH": "", "NREC": str(len(chunk))}
            for j, t in enumerate(chunk, 1):
                params[f"EXCD_{j:02d}"] = excd
                params[f"SYMB_{j:02d}"] = t
            try:
                res = requests.get(
                    f"{_BASE_URL}/uapi/overseas-price/v1/quotations/multprice",
                    headers=self._headers("HHDFS76220000"),
                    params=params,
                    timeout=10,
                )
                out2 = res.json().get("output2", []) or []
                for item in out2:
                    ticker = item.get("symb", "").strip()
                    price  = float(item.get("last", 0) or 0)
                    if ticker and price > 0:
                        prices[ticker] = price
            except Exception as e:
                logger.debug(f"[KIS해외] 복수시세 조회 오류: {e}")
            if i + 10 < len(tickers):
                time.sleep(0.1)

        return prices

    # ── 시세분석 / 스크리너 ───────────────────────────────────────────

    def _ranking_common(self, tr_id: str, url_path: str,
                         extra_params: dict, n: int = 50) -> list[dict]:
        """랭킹 API 공통 호출 헬퍼. output2 → list[dict] 반환."""
        try:
            base_params = {"KEYB": "", "AUTH": ""}
            base_params.update(extra_params)
            res = requests.get(
                f"{_BASE_URL}{url_path}",
                headers=self._headers(tr_id),
                params=base_params,
                timeout=15,
            )
            data  = res.json()
            if data.get("rt_cd") != "0":
                logger.debug(f"[KIS해외] 랭킹 오류({tr_id}): {data.get('msg1','')}")
                return []
            out2 = data.get("output2", []) or []
            results = []
            for item in out2[:n]:
                ticker = (item.get("symb") or "").strip()
                if not ticker or item.get("e_ordyn") == "N":  # 매매불가 제외
                    continue
                results.append({
                    "ticker":  ticker,
                    "name":    (item.get("name") or item.get("ename") or ticker).strip(),
                    "price":   float(item.get("last", 0) or 0),
                    "rate":    float(item.get("rate", 0) or 0),   # 등락율(%)
                    "volume":  int(float(item.get("tvol", 0) or 0)),
                    "rank":    int(item.get("rank", 0) or 0),
                    # 추가 필드 (API마다 다름)
                    "growth_rate": float(item.get("n_rate", 0) or 0),  # 거래증가율
                    "base_diff":   float(item.get("n_diff", 0) or 0),  # 기준가대비
                })
            return results
        except Exception as e:
            logger.debug(f"[KIS해외] 랭킹 조회 실패({tr_id}): {e}")
            return []

    def scan_top_volume(self, exchange: str = "NAS", n: int = 50,
                         min_price: float = 5.0, max_price: float = 0.0) -> list[dict]:
        """거래량순위 (HHDFS76310010) — 당일 거래량 상위 종목."""
        return self._ranking_common(
            tr_id    = "HHDFS76310010",
            url_path = "/uapi/overseas-stock/v1/ranking/trade-vol",
            extra_params = {
                "EXCD":      exchange,
                "NDAY":      "0",       # 당일
                "PRC1":      str(min_price) if min_price > 0 else "",
                "PRC2":      str(max_price) if max_price > 0 else "",
                "VOL_RANG":  "3",       # 1만주 이상
            },
            n = n,
        )

    def scan_trade_growth(self, exchange: str = "NAS", n: int = 50) -> list[dict]:
        """거래증가율순위 (HHDFS76330000) — 거래량이 평소 대비 급증한 종목."""
        return self._ranking_common(
            tr_id    = "HHDFS76330000",
            url_path = "/uapi/overseas-stock/v1/ranking/trade-growth",
            extra_params = {
                "EXCD":      exchange,
                "NDAY":      "0",
                "VOL_RANG":  "3",
            },
            n = n,
        )

    def scan_new_highs(self, exchange: str = "NAS", n: int = 50,
                        period_code: str = "6") -> list[dict]:
        """신고/신저가 (HHDFS76300000) — 52주 신고가 돌파 유지 종목.
        period_code: 6=52주, 5=120일, 4=60일
        """
        return self._ranking_common(
            tr_id    = "HHDFS76300000",
            url_path = "/uapi/overseas-stock/v1/ranking/new-highlow",
            extra_params = {
                "EXCD":      exchange,
                "GUBN":      "1",           # 신고가
                "GUBN2":     "1",           # 돌파유지 (더 강한 신호)
                "NDAY":      period_code,   # 6=52주
                "VOL_RANG":  "3",
            },
            n = n,
        )

    def scan_top_gainers(self, exchange: str = "NAS", n: int = 50) -> list[dict]:
        """상승율순위 (HHDFS76290000) — 당일 상승률 상위 종목."""
        return self._ranking_common(
            tr_id    = "HHDFS76290000",
            url_path = "/uapi/overseas-stock/v1/ranking/updown-rate",
            extra_params = {
                "EXCD":      exchange,
                "GUBN":      "1",   # 상승율
                "NDAY":      "0",   # 당일
                "VOL_RANG":  "3",
            },
            n = n,
        )

    # ── 주문 취소 ─────────────────────────────────────────────────────

    def cancel_order(self, ticker: str, org_odno: str,
                      qty: int, exchange: str = EXCHANGE_NAS) -> bool:
        """미체결 주문 취소 (TTTT1004U). org_odno = 원주문번호."""
        body = {
            "CANO":             self.cano,
            "ACNT_PRDT_CD":     self.acnt_cd,
            "OVRS_EXCG_CD":     exchange,
            "PDNO":             ticker,
            "ORGN_ODNO":        org_odno,
            "RVSE_CNCL_DVSN_CD": "02",   # 취소
            "ORD_QTY":          str(qty),
            "OVRS_ORD_UNPR":    "0",
            "ORD_SVR_DVSN_CD":  "0",
        }
        try:
            res  = requests.post(
                f"{_BASE_URL}/uapi/overseas-stock/v1/trading/order-rvsecncl",
                headers=self._headers("TTTT1004U"),
                data=json.dumps(body),
                timeout=10,
            )
            data  = res.json()
            rt_cd = data.get("rt_cd", "9")
            msg   = data.get("msg1", "")
            if rt_cd == "0":
                logger.info(f"[KIS해외] 주문취소 완료: {ticker} odno={org_odno}")
                return True
            else:
                logger.warning(f"[KIS해외] 주문취소 실패 {ticker}: {msg}")
                return False
        except Exception as e:
            logger.error(f"[KIS해외] 주문취소 통신 오류: {e}")
            return False

    # ── 잔고 조회 ─────────────────────────────────────────────────────

    def get_balance(self) -> dict:
        """
        해외주식 잔고 조회.
        Returns {
          "cash_usd":        float,
          "total_value_usd": float,
          "stocks": [{"ticker", "name", "shares", "avg_price", "current_price", "value"}]
        }
        """
        try:
            res = requests.get(
                f"{_BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance",
                headers=self._headers("TTTS3012R"),
                params={
                    "CANO":           self.cano,
                    "ACNT_PRDT_CD":   self.acnt_cd,
                    "OVRS_EXCG_CD":   "NASD",
                    "TR_CRCY_CD":     "USD",
                    "CTX_AREA_FK200": "",
                    "CTX_AREA_NK200": "",
                },
                timeout=10,
            )
            data    = res.json()
            out1    = data.get("output1", []) or []
            out2    = data.get("output2", {}) or {}

            # 예수금(USD)
            cash_usd  = float(out2.get("frcr_dncl_amt_2", 0) or 0)
            total_val = float(out2.get("tot_evlu_pfls_amt", 0) or 0)

            stocks = []
            for item in out1:
                shares = float(item.get("cblc_qty", 0) or 0)
                if shares <= 0:
                    continue
                stocks.append({
                    "ticker":        item.get("pdno", ""),
                    "name":          item.get("prdt_name", ""),
                    "shares":        shares,
                    "avg_price":     float(item.get("pchs_avg_pric", 0) or 0),
                    "current_price": float(item.get("now_pric2", 0) or 0),
                    "value":         float(item.get("evlu_amt", 0) or 0),
                })

            return {
                "cash_usd":        cash_usd,
                "total_value_usd": total_val,
                "stocks":          stocks,
            }
        except Exception as e:
            logger.error(f"[KIS해외] 잔고 조회 실패: {e}")
            return {"cash_usd": 0.0, "total_value_usd": 0.0, "stocks": []}
