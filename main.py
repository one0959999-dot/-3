import time
import schedule
import yaml
# 🟢 [리팩토링] 회원님 말씀대로 애초에 새로운 kis_brokers 폴더로 직접 찾아가도록 경로를 수정했습니다!
from kis_brokers.kis_real_api import KisRealApi
from kis_brokers.kis_mock_api import KisMockApi
from telegram_bot import TelegramNotifier

kis_instance = None
telegram_instance = None

def load_config(filepath="config.yaml"):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"설정 파일(config.yaml)을 읽는 중 오류가 발생했습니다: {e}")
        return None

def trading_job():
    """정해진 시간(또는 주기)마다 실행될 매매 로직"""
    print("[진행중] 종목 검색 및 자동 매매 로직 실행...")
    
    # 1. 대상 종목 현재가 조회 (예: 삼성전자 '005930')
    target_stock = "005930"
    stock_name = "삼성전자"
    
    if kis_instance and telegram_instance:
        current_price = kis_instance.get_current_price(target_stock)
        
        if current_price:
            msg = f"[{stock_name}] 실시간 현재가: {current_price:,}원"
            print(msg)
            telegram_instance.send_message(msg)
        else:
            print("현재가 조회에 실패했습니다.")

def main():
    global kis_instance, telegram_instance
    
    print("="*50)
    print("라씨 매매비서 스타일 자동 주식 매매 봇 시작")
    print("="*50)
    
    config = load_config()
    if not config:
        return

    # 🟢 [리팩토링] 설정값(is_mock)에 따라 모의투자 API와 실전투자 API를 똑똑하게 갈아 끼웁니다.
    is_mock = config['KIS'].get('IS_MOCK', True)
    if is_mock:
        kis_instance = KisMockApi(
            app_key=config['KIS'].get('APP_KEY', ''),
            app_secret=config['KIS'].get('APP_SECRET', ''),
            account_no=config['KIS'].get('ACCOUNT_NO', '')
        )
    else:
        kis_instance = KisRealApi(
            app_key=config['KIS'].get('APP_KEY', ''),
            app_secret=config['KIS'].get('APP_SECRET', ''),
            account_no=config['KIS'].get('ACCOUNT_NO', '')
        )
    
    # KIS API 토큰 발급 테스트
    kis_instance.get_access_token()
    
    # 텔레그램 연동 객체 생성
    telegram_instance = TelegramNotifier(
        token=config['TELEGRAM'].get('BOT_TOKEN', ''),
        chat_id=config['TELEGRAM'].get('CHAT_ID', '')
    )
    
    telegram_instance.send_message("자동매매 봇이 정상적으로 시작되었습니다.")
    
    # 🛠️ [버그 수정] 전역 schedule 대신 메인 전용 독립 스케줄러 객체를 명시적으로 생성하여 격리
    main_scheduler = schedule.Scheduler()
    main_scheduler.every(10).seconds.do(trading_job)
    
    print("스케줄러가 시작되었습니다. 대기 중...")
    
    try:
        while True:
            # 🛠️ 격리된 메인 스케줄러만 실행하여 BotController 내부 스케줄러와의 상호 간섭을 원천 차단
            main_scheduler.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n프로그램을 종료합니다.")
        telegram_instance.send_message("자동매매 봇이 종료되었습니다.")

if __name__ == "__main__":
    main()
