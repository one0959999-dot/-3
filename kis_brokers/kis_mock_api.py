import time
import requests
import json
import pandas as pd
from datetime import datetime, timedelta, timezone

class KisMockApi:
    """한국투자증권 모의투자 전용 OpenAPI 연동 클래스"""
    
    def __init__(self, app_key: str, app_secret: str, account_no: str):
        self.app_key = app_key
        self.app_secret = app_secret
        self.account_no = account_no.replace('-', '').strip() if account_no else ''
        self.base_url = "https://openapivts.koreainvestment.com:29443"
        self.access_token = None
        self.token_expiry = None
        print(f"[KIS 모의] 모의투자 API 모드 연동 완료 (URL: {self.base_url})")

    def get_access_token(self):
        print("[KIS 모의] 접속 토큰(Access Token) 발급을 요청합니다...")
        url = f"{self.base_url}/oauth2/tokenP"
        headers = {"content-type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=5)
        
        if res.status_code == 200:
            self.access_token = res.json().get('access_token')
            self.token_expiry = datetime.now() + timedelta(hours=23)
            print("[KIS 모의] 토큰 발급 완료! (유효기간 24시간)")
            return self.access_token
        else:
            print(f"[KIS 모의] 토큰 발급 실패: {res.text}")
            return None

    def get_approval_key(self):
        # 모의투자라도 웹소켓키 발급은 반드시 실전 도메인 사용
        real_url = "https://openapi.koreainvestment.com:9443"
        url = f"{real_url}/oauth2/Approval"
        
        headers = {"content-type": "application/json; charset=utf-8"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret
        }
        try:
            res = requests.post(url, headers=headers, data=json.dumps(body), timeout=5)
            if res.status_code == 200:
                approval_key = res.json().get('approval_key')
                print("[KIS 모의] 웹소켓 실시간 인증키(Approval Key) 발급 성공!")
                return approval_key
            else:
                print(f"[KIS 모의] 웹소켓 인증키 발급 실패: {res.text}")
                return None
        except Exception as e:
            print(f"[KIS 모의] 웹소켓 인증키 발급 통신 에러: {e}")
            return None        

    def _ensure_token(self):
        if not self.access_token or not self.token_expiry or datetime.now() >= self.token_expiry:
            return self.get_access_token()
        return self.access_token

    def get_hashkey(self, data: dict):
        url = f"{self.base_url}/uapi/hashkey"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        try:
            res = requests.post(url, headers=headers, data=json.dumps(data), timeout=3)
            if res.status_code == 200:
                return res.json().get("HASH")
            else:
                print(f"[KIS 모의] Hashkey 발급 응답 오류: {res.text}")
        except Exception as e:
            print(f"[KIS 모의] Hashkey 발급 통신 에러: {e}")
        return None

    def _order_headers(self, tr_id: str, hashkey: str) -> dict:
        h = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if hashkey:  # [BUG-FIX] None이면 헤더에서 제외 (kis_real_api와 동일 처리)
            h["hashkey"] = hashkey
        return h
        
    def get_current_price(self, stock_code: str):
        if not self._ensure_token():
            print("[KIS 모의] 접속 토큰이 없어 현재가를 조회할 수 없습니다.")
            return None
            
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": "FHKST01010100"
        }
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code
        }
            
        try:
            res = requests.get(url, headers=headers, params=params, timeout=3)
            if res.status_code == 200:
                data = res.json()
                if data['rt_cd'] == '0':
                    price = int(data['output']['stck_prpr'])
                    return price
                else:
                    print(f"[KIS 모의] 현재가 조회 오류: {data['msg1']}")
                    return None
            else:
                print(f"[KIS 모의] 현재가 조회 통신 실패: {res.text}")
                return None
        except Exception as e:
            print(f"[KIS 모의] 현재가 조회 통신 시간 초과/오류: {e}")
            return None

    def get_realtime_price_data(self, stock_code: str):
        if not self._ensure_token():
            return None
            
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": "FHKST01010100"
        }
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code
        }
            
        try:
            res = requests.get(url, headers=headers, params=params, timeout=3)
            if res.status_code == 200:
                data = res.json()
                if data['rt_cd'] == '0':
                    out = data['output']
                    return {
                        'open': float(out['stck_oprc']),
                        'high': float(out['stck_hgpr']),
                        'low': float(out['stck_lwpr']),
                        'close': float(out['stck_prpr']),
                        'volume': float(out['acml_vol'])
                    }
                return None
            return None
        except Exception as e:
            return None        

    def _place_order(self, stock_code: str, qty: int, side: str, price: int = 0):
        if not self._ensure_token():
            return None

        tr_id = "VTTC0012U" if side == 'BUY' else "VTTC0011U"

        acnt_no   = self.account_no[:8]
        acnt_prdt = self.account_no[8:] if len(self.account_no) > 8 else "01"

        ord_dvsn = "00" if price > 0 else "01"
        
        if price > 0:
            p = int(price)
            if p < 2000:
                tick = 1
            elif p < 5000:
                tick = 5
            elif p < 20000:
                tick = 10
            elif p < 50000:
                tick = 50
            elif p < 200000:
                tick = 100
            elif p < 500000:
                tick = 500
            else:
                tick = 1000
                
            adjusted_price = (p // tick) * tick
            ord_unpr = str(adjusted_price)
        else:
            ord_unpr = "0"

        url  = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        body = {
            "CANO":           acnt_no,
            "ACNT_PRDT_CD":   acnt_prdt,
            "PDNO":           stock_code,
            "ORD_DVSN":       ord_dvsn,   
            "ORD_QTY":        str(qty),
            "ORD_UNPR":       ord_unpr,
        }

        hashkey = self.get_hashkey(body)
        if not hashkey:
            print("[KIS 모의] Hashkey 발급에 실패하여 주문을 취소합니다.")
            return None

        for attempt in range(3):
            try:
                res = requests.post(url, headers=self._order_headers(tr_id, hashkey), data=json.dumps(body), timeout=5)

                if res.status_code == 200:
                    data = res.json()
                    if data.get('rt_cd') == '0':
                        odno = data['output'].get('ODNO', '-')
                        label = '매수' if side == 'BUY' else '매도'
                        order_type_str = '지정가(NXT)' if price > 0 else '최유리지정가(정규장)'
                        print(f"[KIS 모의] {label} 주문 완료 [{order_type_str}] | {stock_code} {qty}주 | 주문번호: {odno}")
                        return data
                    else:
                        msg_cd = data.get('msg_cd', '')
                        print(f"[KIS 모의] 주문 실패: {data.get('msg1', res.text)}")
                        if msg_cd == 'EGW00201':
                            time.sleep(1.2)
                            continue
                        if msg_cd in ('EGW00123', 'EGW00121'):
                            print("[KIS 모의] 토큰 만료 → 재발급 후 재시도")
                            self.access_token = None
                            self._ensure_token()
                            continue
                        return None
                else:
                    print(f"[KIS 모의] 주문 통신 오류: {res.status_code} {res.text}")
                    if 'EGW00201' in res.text:
                        time.sleep(1.2)
                        continue
                    return None
            except Exception as e:
                print(f"[KIS 모의] 주문 요청 통신 시간 초과/오류: {e}")
                return None
        return None
            
    def buy_market_order(self, stock_code: str, qty: int, price: int = 0):
        if qty <= 0:
            return None
        return self._place_order(stock_code, qty, 'BUY', price)
        
    def sell_market_order(self, stock_code: str, qty: int, price: int = 0):
        if qty <= 0:
            return None
        return self._place_order(stock_code, qty, 'SELL', price)

    def get_account_balance(self):
        if not self._ensure_token():
            return None
            
        tr_id = "VTTC8434R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        
        acnt_no = self.account_no[:8]
        acnt_prdt = self.account_no[8:] if len(self.account_no) > 8 else "01"
        
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }
        
        params = {
            "CANO": acnt_no,
            "ACNT_PRDT_CD": acnt_prdt,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "N",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": ""
        }
        
        for retry in range(2):
            try:
                res = requests.get(url, headers=headers, params=params, timeout=3.5)
                
                if res.status_code == 200:
                    data = res.json()
                    if data.get('rt_cd') == '0':
                        stocks = data.get('output1', [])
                        summary = data.get('output2', [{}])[0]
                        
                        parsed_stocks = []
                        manual_total_purchase = 0.0 
                        for s in stocks:
                            if int(s.get('hldg_qty', 0)) > 0:
                                qty = int(s.get('hldg_qty', 0))
                                pchs = float(s.get('pchs_avg_pric', 0))
                                parsed_stocks.append({
                                    "name": s.get('prdt_name', ''),
                                    "ticker": s.get('pdno', ''),
                                    "shares": qty,
                                    "purchase_price": pchs,
                                    "current_price": float(s.get('prpr', 0)),
                                    "value": float(s.get('evlu_amt', 0)),
                                    "profit_rt": float(s.get('evlu_pfls_rt', 0))
                                })
                                manual_total_purchase += (qty * pchs) 
                        
                        def _safe_parse(k1, k2):
                            v1 = summary.get(k1)
                            v2 = summary.get(k2)
                            if v1 and v1 != "0" and v1 != "": return float(v1)
                            if v2 and v2 != "0" and v2 != "": return float(v2)
                            return 0.0

                        api_purchase = _safe_parse('pchs_amt_smtl_amt', 'tot_pchs_amt')
                        final_purchase = api_purchase if api_purchase > 0 else manual_total_purchase

                        return {
                            "stocks": parsed_stocks,
                            "total_cash": float(summary.get('dnca_tot_amt', 0)),
                            "total_value": float(summary.get('tot_evlu_amt', summary.get('scts_evlu_amt', 0))),
                            "total_purchase": final_purchase
                        }
                    else:
                        msg1 = data.get('msg1', '')
                        rt_cd = data.get('rt_cd', '')
                        print(f"[KIS 모의] 잔고 조회 실패: rt_cd={rt_cd}, msg={msg1}, data={data}")
                        if msg1 in ('EGW00123', 'EGW00121'):
                            # 토큰 만료: 재발급 후 retry 루프 continue (재귀 호출 금지 — 무한 스택 방지)
                            self.access_token = None
                            if self._ensure_token():
                                continue   # for retry in range(2) 의 다음 반복으로 재시도
                else:
                    print(f"[KIS 모의] 잔고 조회 통신 오류: status={res.status_code}, text={res.text}")
                return None
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                print(f"[KIS 모의] 잔고조회 타임아웃: {e}")
                return None
        return None

    def search_stock_name(self, query: str):
        query = query.strip()
        if not query:
            return []
            
        try:
            url = "https://ac.finance.naver.com/ac"
            params = {
                "q": query, "st": "111", "r_format": "json", "r_enc": "utf-8",
                "r_unicode": "1", "t_kwd": "expr", "r_lt": "111"
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://finance.naver.com/"
            }
            res = requests.get(url, params=params, headers=headers, timeout=3)
            if res.status_code == 200:
                data = res.json()
                if "items" in data and data["items"] and data["items"][0]:
                    results = []
                    raw_items = data["items"][0]
                    for item in raw_items:
                        if len(item) >= 2:
                            name = item[0]
                            ticker = item[1]
                            if ticker.isdigit() and len(ticker) == 6:
                                results.append({'ticker': ticker, 'name': name})
                    if results:
                        return results
        except Exception as naver_err:
            print(f"⚠️ [네이버 검색망 통신 우회 실패] : {naver_err}")

        if not self._ensure_token():
            return []
        
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/search-stock-info"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": "CTPF1002R",
            "custtype": "P",
        }
        params = {
            "PRDT_TYPE_CD": "300",
            "PDNO": query if query.isdigit() else "",
            "PRDT_NAME": "" if query.isdigit() else query,
            "COND_MRKT_DIV_CODE_1": "J",
            "COND_MRKT_DIV_CODE_2": "Q",
        }
        try:
            res = requests.get(url, headers=headers, params=params, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get('rt_cd') == '0':
                    results = []
                    for item in data.get('output', []):
                        ticker = item.get('pdno', '')
                        name = item.get('prdt_abrv_name', '') or item.get('prdt_name', '')
                        if ticker and name:
                            results.append({'ticker': ticker, 'name': name})
                    return results
        except Exception as e:
            print(f"[KIS 모의] 종목 검색 오류: {e}")
        return []

    def get_volume_rank(self, market_div: str = "J", limit: int = 30):
        if not self._ensure_token():
            return []

        blng = {"J": "1", "Q": "2"}.get(market_div, "0")
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": "FHPST01710000",
            "custtype": "P",
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": blng,
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "000000",
            "FID_INPUT_PRICE_1": "0",
            "FID_INPUT_PRICE_2": "0",
            "FID_VOL_CNT": "0",
            "FID_INPUT_DATE_1": "0",
        }

        try:
            res = requests.get(
                f"{self.base_url}/uapi/domestic-stock/v1/quotations/volume-rank",
                headers=headers, params=params, timeout=5,
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("rt_cd") == "0":
                    return [item["mksc_shrn_iscd"] for item in data.get("output", [])[:limit] if item.get("mksc_shrn_iscd")]
            return []
        except Exception as e:
            print(f"[KIS 모의] 거래량순위 조회 오류: {e}")
            return []

    def get_ohlcv(self, stock_code: str, period: str = "D"):
        if not self._ensure_token():
            return None
            
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": "FHKST03010100"
        }
        
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=180)
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_DATE_1": start_dt.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": end_dt.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": period,
            "FID_ORG_ADJ_PRC": "0"
        }
        
        try:
            res = requests.get(url, headers=headers, params=params, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get('rt_cd') == '0':
                    output2 = data.get('output2', [])
                    if not output2:
                        return pd.DataFrame()
                        
                    df = pd.DataFrame(output2)
                    df = df[['stck_bsop_date', 'stck_oprc', 'stck_hgpr', 'stck_lwpr', 'stck_clpr', 'acml_vol']]
                    df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
                    
                    df['date'] = pd.to_datetime(df['date'])
                    df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
                    
                    df = df.sort_values('date').reset_index(drop=True)
                    return df
            print(f"[KIS 모의] 기간별 시세 조회 실패: {res.text}")
            return pd.DataFrame()
        except Exception as e:
            print(f"[KIS 모의] 기간별 시세 조회 오류: {e}")
            return pd.DataFrame()

    def get_macro_context(self):
        macro_info = []
        try:
            for code, name in [("069500", "KOSPI(KODEX 200)"), ("229200", "KOSDAQ(KODEX 코스닥150)")]:
                price = self.get_current_price(code)
                if price:
                    macro_info.append(f"{name} 대리 지표: {price:,}원")
            
            usd_etf = self.get_current_price("261240")
            if usd_etf:
                macro_info.append(f"원/달러 환율 연동 지표(ETF): {usd_etf:,}원")
        except Exception:
            pass
        return " | ".join(macro_info) if macro_info else "시장 지수 실시간 조회 불가"

    def get_unfilled_orders(self):
        """미체결 주문 내역 조회 (모의투자)"""
        if not self._ensure_token():
            return []
            
        tr_id = "VTTC0084R"  # 모의투자 정정취소가능주문조회 TR_ID
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
        
        acnt_no = self.account_no[:8]
        acnt_prdt = self.account_no[8:] if len(self.account_no) > 8 else "01"
        
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }
        
        params = {
            "CANO": acnt_no,
            "ACNT_PRDT_CD": acnt_prdt,
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
            "INQR_DVSN_1": "0",  # 0:조회순서, 1:주문순
            "INQR_DVSN_2": "0"   # 0:전체, 1:매도, 2:매수
        }
        
        try:
            res = requests.get(url, headers=headers, params=params, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get("rt_cd") == "0":
                    unfilled_list = data.get("output", [])
                    results = []
                    for item in unfilled_list:
                        # rmn_qty 또는 rmnd_qty 필드 처리
                        rem_qty = int(item.get("rmn_qty", 0)) if item.get("rmn_qty") else int(item.get("rmnd_qty", 0))
                        if rem_qty > 0:
                            results.append({
                                "order_no": item.get("odno"),
                                "ticker": item.get("pdno"),
                                "name": item.get("prdt_name"),
                                "order_qty": int(item.get("ord_qty", 0)),
                                "rem_qty": rem_qty,
                                "order_price": float(item.get("ord_unpr", 0)),
                                "side": "SELL" if item.get("sll_buy_dvsn_cd") == "01" else "BUY",
                                "ord_gno_brno": item.get("ord_gno_brno", "")
                            })
                    return results
            return []
        except Exception as e:
            print(f"[KIS 모의] 미체결 조회 오류: {e}")
            return []

    def cancel_order(self, org_order_no: str, stock_code: str, rem_qty: int, krx_fwdg_ord_orgno: str = ""):
        """미체결 주문 취소 (모의투자)"""
        if not self._ensure_token():
            return None

        tr_id = "VTTC0013U"  # 모의투자 정정취소주문 TR_ID
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-rvsecncl"

        acnt_no = self.account_no[:8]
        acnt_prdt = self.account_no[8:] if len(self.account_no) > 8 else "01"

        body = {
            "CANO": acnt_no,
            "ACNT_PRDT_CD": acnt_prdt,
            "KRX_FWDG_ORD_ORGNO": krx_fwdg_ord_orgno,
            "ORGN_ORD_NO": org_order_no,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02", # 02: 취소
            "ORD_QTY": str(rem_qty),
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y"
        }
        
        hashkey = self.get_hashkey(body)
        if not hashkey:
            return None

        try:
            res = requests.post(url, headers=self._order_headers(tr_id, hashkey), data=json.dumps(body), timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get('rt_cd') == '0':
                    print(f"[KIS 모의] 주문취소 완료 | 원주문번호: {org_order_no}")
                else:
                    print(f"[KIS 모의] 주문취소 거부: {data.get('msg1')}")
                return data
            return None
        except Exception as e:
            print(f"[KIS 모의] 주문취소 통신 오류: {e}")
            return None

    def cancel_all_unfilled_orders(self):
        """계좌 내 모든 미체결 주문 일괄 취소 (모의투자)"""
        unfilled_orders = self.get_unfilled_orders()
        if not unfilled_orders:
            return True
            
        print(f"[KIS 모의] 총 {len(unfilled_orders)}건의 미체결 주문 취소를 시작합니다.")
        success_count = 0
        for order in unfilled_orders:
            res = self.cancel_order(order['order_no'], order['ticker'], order['rem_qty'], order.get('ord_gno_brno', ''))
            if res and res.get('rt_cd') == '0':
                success_count += 1
            time.sleep(0.2)

        print(f"[KIS 모의] 미체결 일괄 취소 완료 ({success_count}/{len(unfilled_orders)}건)")
        return success_count == len(unfilled_orders)

    def get_order_fills(self, date_str: str = ""):
        """주식일별주문체결조회 (모의투자) — date_str: YYYYMMDD, 미입력시 오늘"""
        if not self._ensure_token():
            return []

        if not date_str:
            kst = datetime.now(tz=timezone(timedelta(hours=9)))
            date_str = kst.strftime("%Y%m%d")

        tr_id = "VTTC0081R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-daily-ccld"

        acnt_no = self.account_no[:8]
        acnt_prdt = self.account_no[8:] if len(self.account_no) > 8 else "01"

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }

        params = {
            "CANO": acnt_no,
            "ACNT_PRDT_CD": acnt_prdt,
            "INQR_STRT_DT": date_str,
            "INQR_END_DT": date_str,
            "SLL_BUY_DVSN_CD": "00",
            "INQR_DVSN": "00",
            "PDNO": "",
            "CCLD_DVSN": "00",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        try:
            res = requests.get(url, headers=headers, params=params, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get("rt_cd") == "0":
                    output1 = data.get("output1", [])
                    results = []
                    for item in output1:
                        results.append({
                            "order_no": item.get("odno"),
                            "ticker": item.get("pdno"),
                            "name": item.get("prdt_name"),
                            "side": "SELL" if item.get("sll_buy_dvsn_cd") == "01" else "BUY",
                            "order_qty": int(item.get("ord_qty", 0)),
                            "filled_qty": int(item.get("tot_ccld_qty", 0)),
                            "rem_qty": int(item.get("rmn_qty", 0)),
                            "avg_price": float(item.get("avg_prvs", 0)),
                            "order_price": float(item.get("ord_unpr", 0)),
                            "order_time": item.get("ord_tmd", ""),
                        })
                    return results
            print(f"[KIS 모의] 주문체결조회 실패: {res.text}")
            return []
        except Exception as e:
            print(f"[KIS 모의] 주문체결조회 오류: {e}")
            return []

    def get_buyable_cash(self, stock_code: str = "", price: int = 0):
        """매수가능조회 (모의투자) — 매수 가능 금액(원) 반환"""
        if not self._ensure_token():
            return 0

        tr_id = "VTTC8908R"
        url = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-order"

        acnt_no = self.account_no[:8]
        acnt_prdt = self.account_no[8:] if len(self.account_no) > 8 else "01"

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }

        params = {
            "CANO": acnt_no,
            "ACNT_PRDT_CD": acnt_prdt,
            "PDNO": stock_code,
            "ORD_UNPR": str(price) if price > 0 else "0",
            "ORD_DVSN": "00" if price > 0 else "01",
            "CMA_EVLU_AMT_ICLD_YN": "Y",
            "OVRS_ICLD_YN": "N",
        }

        try:
            res = requests.get(url, headers=headers, params=params, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get("rt_cd") == "0":
                    output = data.get("output", {})
                    return int(output.get("nrcvb_buy_amt", 0))
            print(f"[KIS 모의] 매수가능조회 실패: {res.text}")
            return 0
        except Exception as e:
            print(f"[KIS 모의] 매수가능조회 오류: {e}")
            return 0

    def get_orderbook(self, stock_code: str, market: str = "J"):
        """주식현재가 호가_예상체결 조회 — askp1(최우선 매도호가), bidp1(최우선 매수호가) 반환"""
        if not self._ensure_token():
            return None

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": "FHKST01010200",
            "custtype": "P",
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": stock_code,
        }

        try:
            res = requests.get(
                f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
                headers=headers, params=params, timeout=5,
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("rt_cd") == "0":
                    o = data.get("output1", {})
                    return {
                        "askp1": int(o.get("askp1", 0) or 0),
                        "bidp1": int(o.get("bidp1", 0) or 0),
                        "askp_rsqn1": int(o.get("askp_rsqn1", 0) or 0),
                        "bidp_rsqn1": int(o.get("bidp_rsqn1", 0) or 0),
                    }
            return None
        except Exception as e:
            print(f"[KIS 모의] 호가조회 오류: {e}")
            return None

    def get_minute_candles(self, stock_code: str, count: int = 10, market: str = "J"):
        """주식당일분봉조회 — 최근 count개 분봉 반환 (당일만 제공, 1회 최대 30개)"""
        if not self._ensure_token():
            return []

        kst = datetime.now(tz=timezone(timedelta(hours=9)))
        hour_str = kst.strftime("%H%M%S")

        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": "FHKST03010200",
            "custtype": "P",
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_HOUR_1": hour_str,
            "FID_PW_DATA_INCU_YN": "Y",
            "FID_ETC_CLS_CODE": "",
        }

        try:
            res = requests.get(
                f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
                headers=headers, params=params, timeout=5,
            )
            if res.status_code == 200:
                data = res.json()
                if data.get("rt_cd") == "0":
                    output2 = data.get("output2", [])
                    results = []
                    for item in output2[:count]:
                        results.append({
                            "time": item.get("stck_cntg_hour", ""),
                            "open": int(item.get("stck_oprc", 0) or 0),
                            "high": int(item.get("stck_hgpr", 0) or 0),
                            "low": int(item.get("stck_lwpr", 0) or 0),
                            "close": int(item.get("stck_prpr", 0) or 0),
                            "volume": int(item.get("cntg_vol", 0) or 0),
                        })
                    # KIS API는 최신봉 먼저(내림차순) 반환 → 역순 정렬해 최신봉이 마지막이 되게 함
                    # check_giveback_stop/_check_minute_trend_up 등 모든 호출부가 candles[-1]=최신봉 가정
                    return results[::-1]
            return []
        except Exception as e:
            print(f"[KIS 모의] 분봉조회 오류: {e}")
            return []

    def get_price_change_rank(self, market_div: str = "J", limit: int = 30):
        """모의투자 미지원 — 빈 리스트 반환"""
        return []

    def get_foreign_institution_rank(self, market_div: str = "J", limit: int = 30):
        """모의투자 미지원 — 빈 리스트 반환"""
        return []

    def get_etf_price(self, etf_code: str):
        """모의투자 미지원 — 항상 None 반환"""
        return None

    def get_sellable_qty(self, stock_code: str):
        """모의투자 미지원 — 항상 0 반환"""
        return 0