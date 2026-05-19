# pyrefly: ignore [missing-import]
import webview
import threading
from app import app

def start_flask_server():
    # Flask 서버를 5000 포트에서 백그라운드로 구동 (디버그 모드 끔)
    app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)

if __name__ == '__main__':
    # 데몬 스레드로 실행 (앱 종료 시 서버도 함께 종료되도록)
    t = threading.Thread(target=start_flask_server, daemon=True)
    t.start()
    
    # 웹뷰 창 생성
    window = webview.create_window(
        title='라씨 자동 매매비서', 
        url='http://127.0.0.1:5000',
        width=1000,
        height=800,
        resizable=True
    )
    
    # 창 띄우기 (실행 시 메인 스레드 점유)
    webview.start()
