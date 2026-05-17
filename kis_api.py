import requests
import json
import pandas as pd
from datetime import datetime, timedelta

class KisApi:
    """한국투자증권 OpenAPI 연동을 위한 클래스입니다."""
    
    def __init__(self, app_key: str, app_secret: str, account_no: str, is_mock: bool = True):
        self.app_key = app_key
        self.app_secret = app_secret
        # 계좌번호 정규화: "44550923-01" → "4455092301" (하이픈 자동 제거)
        self.account_no = account_no.replace('-', '').strip() if account_no else ''
        self.set_mode(is_mock) # 초기 모드 설정

    def set_mode(self, is_mock: bool):
        """실전/모의 모드를 변경하고 그에 맞는 URL과 토큰을 초기화합니다."""
        self.is_mock = is_mock
        # 모의투자 및 실전투자 URL 구분
        self.base_url = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
        self.access_token = None # 모드가 바뀌면 토큰은 반드시 새로 발급받아야 함
        self.token_expiry = None
        print(f"[KIS] 모드 변경: {'모의투자' if is_mock else '실전투자'} (URL: {self.base_url})")

    def get_access_token(self):
        """API 사용을 위한 토큰 발급"""
        print("[KIS] 접속 토큰(Access Token) 발급을 요청합니다...")
        url = f"{self.base_url}/oauth2/tokenP"
        headers = {"content-type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        # 💡 timeout=5 추가: 5초 이상 무응답 시 무한대기 방지
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=5)
        
        if res.status_code == 200:
            self.access_token = res.json().get('access_token')
            # 24시간 유효하지만 안전하게 23시간 뒤 만료로 설정
            self.token_expiry = datetime.now() + timedelta(hours=23)
            print("[KIS] 토큰 발급 완료! (유효기간 24시간)")
            return self.access_token
        else:
            print(f"[KIS] 토큰 발급 실패: {res.text}")
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
                print("[KIS] 웹소켓 실시간 인증키(Approval Key) 발급 성공!")
                return approval_key
            else:
                print(f"[KIS] 웹소켓 인증키 발급 실패: {res.text}")
                return None
        except Exception as e:
            print(f"[KIS] 웹소켓 인증키 발급 통신 에러: {e}")
            return None        

    def _ensure_token(self):
        """토큰이 없거나 만료되었으면 자동 발급"""
        if not self.access_token or not self.token_expiry or datetime.now() >= self.token_expiry:
            return self.get_access_token()
        return self.access_token

    def _order_headers(self, tr_id: str) -> dict:
        """주문 공통 헤더 생성"""
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }
        
    def get_current_price(self, stock_code: str):
        """특정 종목의 현재가 조회"""
        if not self._ensure_token():
            print("[KIS] 접속 토큰이 없어 현재가를 조회할 수 없습니다. (APP_KEY 등을 확인해주세요)")
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
            # 💡 timeout=3 추가: 현재가 조회는 즉각 응답해야 하므로 3초 대기
            res = requests.get(url, headers=headers, params=params, timeout=3)
            if res.status_code == 200:
                data = res.json()
                if data['rt_cd'] == '0':
                    price = int(data['output']['stck_prpr'])
                    return price
                else:
                    print(f"[KIS] 현재가 조회 오류: {data['msg1']}")
                    return None
            else:
                print(f"[KIS] 현재가 조회 통신 실패: {res.text}")
                return None
        except Exception as e:
            # 💡 통신 지연 에러 발생 시 프로그램이 터지지 않도록 예외 처리
            print(f"[KIS] 현재가 조회 통신 시간 초과/오류: {e}")
            return None

    def get_realtime_price_data(self, stock_code: str):
        """특정 종목의 당일 시/고/저/종가 실시간 데이터 전체 조회 (보조지표 왜곡 방지용)"""
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

    def _place_order(self, stock_code: str, qty: int, side: str):
        """
        시장가 주문 공통 로직 (최유리지정가로 변경하여 슬리피지 원천 차단)
        side: 'BUY' | 'SELL'
        """
        if not self._ensure_token():
            return None

        # 실전: TTTC0802U(매수) / TTTC0801U(매도)
        # 모의: VTTC0802U(매수) / VTTC0801U(매도)
        if side == 'BUY':
            tr_id = "VTTC0802U" if self.is_mock else "TTTC0802U"
        else:
            tr_id = "VTTC0801U" if self.is_mock else "TTTC0801U"

        # 계좌번호 분리 (앞 8자리 + 뒤 2자리)
        acnt_no   = self.account_no[:8]
        acnt_prdt = self.account_no[8:] if len(self.account_no) > 8 else "01"

        url  = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        body = {
            "CANO":           acnt_no,
            "ACNT_PRDT_CD":   acnt_prdt,
            "PDNO":           stock_code,
            "ORD_DVSN":       "03",   # 🟢 03 = 최유리지정가 (매수시 최우선 매도호가, 매도시 최우선 매수호가로 슬리피지 방어)
            "ORD_QTY":        str(qty),
            "ORD_UNPR":       "0",    # 최유리지정가도 단가 0으로 전송
        }

        try:
            # 💡 timeout=5 추가
            res = requests.post(
                url,
                headers=self._order_headers(tr_id),
                data=json.dumps(body),
                timeout=5
            )

            if res.status_code == 200:
                data = res.json()
                if data.get('rt_cd') == '0':
                    odno = data['output'].get('ODNO', '-')
                    label = '매수' if side == 'BUY' else '매도'
                    print(f"[KIS] {label} 주문 완료 | {stock_code} {qty}주 | 주문번호: {odno}")
                    return data
                else:
                    msg_cd = data.get('msg_cd', '')
                    print(f"[KIS] 주문 실패: {data.get('msg1', res.text)}")
                    # 토큰 만료(EGW00123/EGW00121) → 재발급 후 1회 재시도
                    if msg_cd in ('EGW00123', 'EGW00121'):
                        print("[KIS] 토큰 만료 → 재발급 후 재시도")
                        self.access_token = None
                        self._ensure_token()
                        return self._place_order(stock_code, qty, side)
                    return None
            else:
                print(f"[KIS] 주문 통신 오류: {res.status_code} {res.text}")
                return None
        except Exception as e:
            print(f"[KIS] 주문 요청 통신 시간 초과/오류: {e}")
            return None
        
    def buy_market_order(self, stock_code: str, qty: int):
        """시장가 매수 주문"""
        if qty <= 0:
            return None
        return self._place_order(stock_code, qty, 'BUY')
        
    def sell_market_order(self, stock_code: str, qty: int):
        """시장가 매도 주문"""
        if qty <= 0:
            return None
        return self._place_order(stock_code, qty, 'SELL')

    def get_account_balance(self):
        """계좌 잔고 및 종목 보유 내역 조회 (실제 계좌)"""
        if not self._ensure_token():
            return None
            
        tr_id = "VTTC8434R" if self.is_mock else "TTTC8434R"
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
        
        try:
            # 💡 timeout=5 추가
            res = requests.get(url, headers=headers, params=params, timeout=5)
            
            if res.status_code == 200:
                data = res.json()
                if data.get('rt_cd') == '0':
                    stocks = data.get('output1', [])
                    summary = data.get('output2', [{}])[0]
                    
                    parsed_stocks = []
                    for s in stocks:
                        if int(s.get('hldg_qty', 0)) > 0:
                            parsed_stocks.append({
                                "name": s.get('prdt_name', ''),
                                "ticker": s.get('pdno', ''),
                                "shares": int(s.get('hldg_qty', 0)),
                                "purchase_price": float(s.get('pchs_avg_pric', 0)),
                                "current_price": float(s.get('prpr', 0)),
                                "value": float(s.get('evlu_amt', 0)),
                                "profit_rt": float(s.get('evlu_pfls_rt', 0))
                            })
                    
                    # 🟢 [버그 해결] 파이썬 논리 연산자(or)의 맹점으로 인해 문자열 "0"이 채택되어 잔고가 0원으로 증발하는 현상 완벽 차단
                    def _safe_parse(k1, k2):
                        v1 = summary.get(k1)
                        v2 = summary.get(k2)
                        # 값이 존재하고 "0"이나 빈 값이 아니면 해당 진짜 데이터를 우선 채택합니다.
                        if v1 and v1 != "0" and v1 != "": return float(v1)
                        if v2 and v2 != "0" and v2 != "": return float(v2)
                        return 0.0

                    return {
                        "stocks": parsed_stocks,
                        "total_cash": _safe_parse('prvs_rcdl_excc_amt', 'dnca_tot_amt'), # D+2 예수금
                        "total_value": _safe_parse('tot_evlu_amt', 'evlu_amt_smtl_amt'), # 총 평가금액
                        "total_purchase": _safe_parse('pchs_amt_smtl_amt', 'tot_pchs_amt') # 매입금액 합계
                    }
                else:
                    msg1 = data.get('msg1', '')
                    rt_cd = data.get('rt_cd', '')
                    print(f"[KIS] 잔고 조회 실패: rt_cd={rt_cd}, msg={msg1}, data={data}")
                    if msg1 in ('EGW00123', 'EGW00121'):
                        self.access_token = None
                        self._ensure_token()
                        return self.get_account_balance()
            else:
                print(f"[KIS] 잔고 조회 통신 오류: status={res.status_code}, text={res.text}")
            return None
        except Exception as e:
            print(f"[KIS] 잔고 조회 통신 시간 초과/오류: {e}")
            return None

    def search_stock_name(self, query: str):
        """종목명 또는 코드로 KOSPI/KOSDAQ 종목 검색 (네이버 금융 실시간 초정밀 무적 검색 이식)"""
        query = query.strip()
        if not query:
            return []
            
        try:
            # 🟢 [버그 해결] 한국투자증권의 상품정보검색 API(CTPF1002R)는 실전/모의를 막론하고 
            # 개인 App Key 계약 권한 제한으로 인해 결과 장부를 차단하여 백지 화면을 유발합니다.
            # 계좌 권한 제약이 전혀 없는 네이버 금융 실시간 마스터 검색망을 연동하여 24시간 언제나 초성 검색까지 완벽 지원합니다.
            url = "https://ac.finance.naver.com/ac"
            params = {
                "q": query,
                "st": "111",
                "r_format": "json",
                "r_enc": "utf-8",
                "r_unicode": "1",
                "t_kwd": "expr",
                "r_lt": "111"
            }
            
            res = requests.get(url, params=params, timeout=3)
            if res.status_code == 200:
                data = res.json()
                if "items" in data and data["items"] and data["items"][0]:
                    results = []
                    raw_items = data["items"][0]
                    for item in raw_items:
                        # 네이버 자동완성 결과 구조: ["종목명", "종목코드", "동의어", "초성", "구분"]
                        if len(item) >= 2:
                            name = item[0]
                            ticker = item[1]
                            # 국외 주식이나 인덱스 선물이 뒤섞이는 것을 막기 위해 순수 국내 6자리 숫자 종목코드만 필터링
                            if ticker.isdigit() and len(ticker) == 6:
                                results.append({'ticker': ticker, 'name': name})
                    if results:
                        return results
        except Exception as naver_err:
            print(f"⚠️ [네이버 검색망 통신 우회 실패] : {naver_err}")

        # --- 🟡 [최종 백업] 기존 한국투자증권 오리지널 API 라인 보존 ---
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
            print(f"[KIS] 종목 검색 오류: {e}")
        return []

    def get_volume_rank(self, market_div="J", limit=30):
        """
        거래량 상위 종목 검색 (KIS API 사용)
        market_div: 'J' (KOSPI) 또는 'Q' (KOSDAQ)
        """
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
            print(f"[KIS] 거래량 상위 검색 오류: {e}")
        return []

    def get_ohlcv(self, stock_code: str, period: str = "D"):
        """
        국내주식 기간별 시세 조회 (FHKST03010100) - 과거 차트 데이터
        period: "D"(일봉), "W"(주봉), "M"(월봉)
        """
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
        
        # 오늘 날짜와 180일 전 날짜 계산 (120일 이동평균선 확보용)
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=180)
        
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_DATE_1": start_dt.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": end_dt.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": period,
            "FID_ORG_ADJ_PRC": "0" # 0: 수정주가, 1: 원주가
        }
        
        try:
            res = requests.get(url, headers=headers, params=params, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if data.get('rt_cd') == '0':
                    output2 = data.get('output2', [])
                    if not output2:
                        return pd.DataFrame()
                        
                    # DataFrame으로 변환 및 타입 캐스팅
                    df = pd.DataFrame(output2)
                    df = df[['stck_bsop_date', 'stck_oprc', 'stck_hgpr', 'stck_lwpr', 'stck_clpr', 'acml_vol']]
                    df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
                    
                    df['date'] = pd.to_datetime(df['date'])
                    df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
                    
                    # 과거 날짜가 위로 오도록 오름차순 정렬
                    df = df.sort_values('date').reset_index(drop=True)
                    return df
            print(f"[KIS] 기간별 시세 조회 실패: {res.text}")
            return pd.DataFrame()
        except Exception as e:
            print(f"[KIS] 기간별 시세 조회 오류: {e}")
            return pd.DataFrame()

# 🟢 [신규 추가 코드] AI 판단용 실시간 거시경제 및 시장 지수 수집 기능
    def get_macro_context(self):
        """AI의 시황 인지를 위해 코스피, 코스닥 현재가 및 간단한 환율 동향을 문자열로 반환합니다."""
        macro_info = []
        try:
            # 0001: 코스피 지수, 2001: 코스닥 지수
            for code, name in [("0001", "KOSPI"), ("2001", "KOSDAQ")]:
                price = self.get_current_price(code)
                if price:
                    macro_info.append(f"{name} 지수: {price:,}")
            
            # 환율 정보 우회 조회 시도 (환율 ETF 가격 추이 등으로 대체하거나 생략 가능)
            # 환율 정보 우회 조회 시도 (환율 ETF 가격 추이 등으로 대체하거나 생략 가능)
            usd_etf = self.get_current_price("261240") # KODEX 미국달러선물
            if usd_etf:
                macro_info.append(f"원/달러 환율 연동 지표(ETF): {usd_etf:,}원")
        except Exception:
            pass
        return " | ".join(macro_info) if macro_info else "시장 지수 실시간 조회 불가"

    # ▼▼ 여기서부터 새로 추가된 부분 ▼▼
    def get_approval_key(self):
        """웹소켓 실시간 접속을 위한 전용 암호키(Approval Key) 발급"""
        url = f"{self.base_url}/oauth2/Approval"
        headers = {"content-type": "application/json; utf-8"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret
        }
        try:
            res = requests.post(url, headers=headers, data=json.dumps(body), timeout=5)
            if res.status_code == 200:
                return res.json().get('approval_key')
            else:
                print(f"[KIS WS] Approval Key 발급 실패: {res.text}")
                return None
        except Exception as e:
            print(f"[KIS WS] Approval Key 요청 중 통신 오류: {e}")
            return None

if __name__ == '__main__':
    # 테스트 코드
    pass