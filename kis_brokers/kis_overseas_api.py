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
        """시장가 매수"""
        return self._order(ticker, qty, "BUY", exchange)

    def sell_market_order(self, ticker: str, qty: int,
                          exchange: str = EXCHANGE_NAS) -> bool:
        """시장가 매도"""
        return self._order(ticker, qty, "SELL", exchange)

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
