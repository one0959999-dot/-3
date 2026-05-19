import requests
import json
import pandas as pd
from datetime import datetime, timedelta

class KisRealApi:
    """한국투자증권 실전투자 전용 OpenAPI 연동 클래스"""
    
    def __init__(self, app_key: str, app_secret: str, account_no: str):
        self.app_key = app_key
        self.app_secret = app_secret
        self.account_no = account_no.replace('-', '').strip() if account_no else ''
        self.base_url = "https://openapi.koreainvestment.com:9443"
        self.access_token = None
        self.token_expiry = None
        print(f"[KIS 실전] 실전투자 API 모드 연동 완료 (URL: {self.base_url})")

    def get_access_token(self):
        """API 사용을 위한 토큰 발급"""
        print("[KIS 실전] 접속 토큰(Access Token) 발급을 요청합니다...")
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
            print("[KIS 실전] 토큰 발급 완료! (유효기간 24시간)")
            return self.access_token
        else:
            print(f"[KIS 실전] 토큰 발급 실패: {res.text}")
            return None

    def get_approval_key(self):
        """웹소켓 실시간 접속을 위한 웹소켓용 Approval Key 발급"""
        url = f"{self.base_url}/oauth2/Approval"
        
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
                print("[KIS 실전] 웹소켓 실시간 인증키(Approval Key) 발급 성공!")
                return approval_key
            else:
                print(f"[KIS 실전] 웹소켓 인증키 발급 실패: {res.text}")
                return None
        except Exception as e:
            print(f"[KIS 실전] 웹소켓 인증키 발급 통신 에러: {e}")
            return None        

    def _ensure_token(self):
        """토큰이 없거나 만료되었으면 자동 발급"""
        if not self.access_token or not self.token_expiry or datetime.now() >= self.token_expiry:
            return self.get_access_token()
        return self.access_token

    def get_hashkey(self, data: dict):
        """POST 요청(주문 등)에 필수적인 HASHKEY 발급"""
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
                print(f"[KIS 실전] Hashkey 발급 응답 오류: {res.text}")
        except Exception as e:
            print(f"[KIS 실전] Hashkey 발급 통신 에러: {e}")
        return None

    def _order_headers(self, tr_id: str, hashkey: str) -> dict:
        """주문 공통 헤더 생성"""
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
            "hashkey": hashkey
        }
        
    def get_current_price(self, stock_code: str):
        """특정 종목의 현재가 조회"""
        if not self._ensure_token():
            print("[KIS 실전] 접속 토큰이 없어 현재가를 조회할 수 없습니다.")
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
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code
        }
            
        try:
            res = requests.get(url, headers=headers, params=params, timeout=3)
            if res.status_code == 200:
                data = res.json()
                if data['rt_cd'] == '0':
                    price = int(data['output']['stck_prpr'])
                    return price
                else:
                    print(f"[KIS 실전] 현재가 조회 오류: {data['msg1']}")
                    return None
            else:
                print(f"[KIS 실전] 현재가 조회 통신 실패: {res.text}")
                return None
        except Exception as e:
            print(f"[KIS 실전] 현재가 조회 통신 시간 초과/오류: {e}")
            return None

    def get_realtime_price_data(self, stock_code: str):
        """특정 종목의 당일 시/고/저/종가 실시간 데이터 전체 조회"""
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
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code
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
        """시장가/지정가 하이브리드 주문 로직 (실전)"""
        if not self._ensure_token():
            return None

        if side == 'BUY':
            tr_id = "TTTC0802U"
        else:
            tr_id = "TTTC0801U"

        acnt_no   = self.account_no[:8]
        acnt_prdt = self.account_no[8:] if len(self.account_no) > 8 else "01"

        ord_dvsn = "00" if price > 0 else "03"
        
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
            print("[KIS 실전] Hashkey 발급에 실패하여 주문을 취소합니다.")
            return None

        try:
            res = requests.post(url, headers=self._order_headers(tr_id, hashkey), data=json.dumps(body), timeout=5)

            if res.status_code == 200:
                data = res.json()
                if data.get('rt_cd') == '0':
                    odno = data['output'].get('ODNO', '-')
                    label = '매수' if side == 'BUY' else '매도'
                    order_type_str = '지정가(NXT)' if price > 0 else '최유리지정가(정규장)'
                    print(f"[KIS 실전] {label} 주문 완료 [{order_type_str}] | {stock_code} {qty}주 | 주문번호: {odno}")
                    return data
                else:
                    msg_cd = data.get('msg_cd', '')
                    print(f"[KIS 실전] 주문 실패: {data.get('msg1', res.text)}")
                    if msg_cd in ('EGW00123', 'EGW00121'):
                        print("[KIS 실전] 토큰 만료 → 재발급 후 재시도")
                        self.access_token = None
                        self._ensure_token()
                        return self._place_order(stock_code, qty, side, price)
                    return None
            else:
                print(f"[KIS 실전] 주문 통신 오류: {res.status_code} {res.text}")
                return None
        except Exception as e:
            print(f"[KIS 실전] 주문 요청 통신 시간 초과/오류: {e}")
            return None
            
    def buy_market_order(self, stock_code: str, qty: int, price: int = 0):
        """시장가/지정가 하이브리드 매수 주문"""
        if qty <= 0:
            return None
        return self._place_order(stock_code, qty, 'BUY', price)
        
    def sell_market_order(self, stock_code: str, qty: int, price: int = 0):
        """시장가/지정가 하이브리드 매도 주문"""
        if qty <= 0:
            return None
        return self._place_order(stock_code, qty, 'SELL', price)

    def get_account_balance(self):
        """계좌 잔고 및 종목 보유 내역 조회 (실전 계좌)"""
        if not self._ensure_token():
            return None
            
        tr_id = "TTTC8434R"
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
            "cano": acnt_no,
            "acnt_prdt_cd": acnt_prdt,
            "afhr_flpr_yn": "N",
            "ofl_yn": "N",
            "inqr_dvsn": "02",
            "unpr_dvsn": "01",
            "fund_sttl_icld_yn": "N",
            "fncg_amt_auto_rdpt_yn": "N",
            "prcs_dvsn": "00",
            "ctx_area_fk100": "",
            "ctx_area_nk100": ""
        }
        
        try:
            res = requests.get(url, headers=headers, params=params, timeout=5)
            
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
                        "total_cash": _safe_parse('prvs_rcdl_excc_amt', 'dnca_tot_amt'),
                        "total_value": _safe_parse('scts_evlu_amt', 'evlu_amt_smtl_amt'),
                        "total_purchase": final_purchase
                    }
                else:
                    msg1 = data.get('msg1', '')
                    rt_cd = data.get('rt_cd', '')
                    print(f"[KIS 실전] 잔고 조회 실패: rt_cd={rt_cd}, msg={msg1}, data={data}")
                    if msg1 in ('EGW00123', 'EGW00121'):
                        self.access_token = None
                        self._ensure_token()
                        return self.get_account_balance()
            else:
                print(f"[KIS 실전] 잔고 조회 통신 오류: status={res.status_code}, text={res.text}")
            return None
        except Exception as e:
            print(f"[KIS 실전] 잔고 조회 통신 시간 초과/오류: {e}")
            return None

    def search_stock_name(self, query: str):
        """종목명 또는 코드로 KOSPI/KOSDAQ 종목 검색"""
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
            print(f"[KIS 실전] 종목 검색 오류: {e}")
        return []

    def get_volume_rank(self, market_div="J", limit=30):
        """거래량 상위 종목 검색"""
        if not self._ensure_token():
            return []
            
        url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/volume-rank"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": "FHPST01710000",
            "custtype": "P",
        }
        params = {
            "FID_COND_MRKT_DIV_CODE": market_div,
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "0",
            "FID_TRGT_CLS_CODE": "111111111",
            "FID_TRGT_EXLS_CLS_CODE": "111111",
            "FID_INPUT_PRICE_1": "1000",
            "FID_INPUT_PRICE_2": "1000000",
            "FID_VOL_CNT": "100000",
            "FID_INPUT_DATE_1": ""
        }
        
        try:
            res = requests.get(url, headers=headers, params=params, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get('rt_cd') == '0':
                    tickers = []
                    for idx, item in enumerate(data.get('output', [])):
                        if idx >= limit: break
                        ticker = item.get('mksc_shrn_iscd')
                        if ticker:
                            tickers.append(ticker)
                    return tickers
        except Exception as e:
            print(f"[KIS 실전] 거래량 상위 검색 오류: {e}")
        return []

    def get_ohlcv(self, stock_code: str, period: str = "D"):
        """국내주식 기간별 시세 조회 (과거 차트 데이터)"""
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
            print(f"[KIS 실전] 기간별 시세 조회 실패: {res.text}")
            return pd.DataFrame()
        except Exception as e:
            print(f"[KIS 실전] 기간별 시세 조회 오류: {e}")
            return pd.DataFrame()

    def get_macro_context(self):
        """AI 판단용 실시간 거시경제 및 시장 지수 수집"""
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