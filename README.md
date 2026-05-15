# 라씨 매매비서 (Lassi Trading Bot)

AI 기반 한국투자증권 자동 매매 시스템입니다. Core-Satellite 전략을 사용하여 안정적인 수익과 공격적인 운용을 병행합니다.

## 주요 기능

- **자동 매매**: 한국투자증권 API를 통한 실시간 주식 매매
- **Core-Satellite 전략**: 
    - **Core**: 우량주 중심의 안정적 보유 (예: 보령)
    - **Satellite**: AI 스크리닝을 통한 시장 주도주 단기 매매
- **AI 시장 분석**: Google Gemini API를 활용한 시장 상황 분석 및 리포트 생성
- **실시간 대시보드**: Flask 기반의 웹 인터페이스로 계좌 상태 및 매매 현황 모니터링
- **텔레그램 알림**: 주요 매매 발생 시 텔레그램으로 즉시 알림 전송

## 설치 및 실행 방법

1. **저장소 복제**
   ```bash
   git clone https://github.com/one0959999-dot/-2.git
   cd lassi_bot
   ```

2. **가상환경 설정 및 패키지 설치**
   ```bash
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **설정 파일 작성**
   `config.yaml` 파일을 생성하고 한국투자증권 API 키와 텔레그램 봇 정보를 입력합니다. (예시 파일 `config.yaml.example` 참고)

4. **실행**
   ```bash
   python app.py
   ```

## 주의 사항
- 본 프로그램은 투자 판단의 참고용이며, 투자에 대한 책임은 본인에게 있습니다.
- API 키와 같은 민감한 정보는 절대 공용 저장소에 올리지 마십시오.
