"""
claude_api.py
라씨봇 AI 엔진 — Claude(Anthropic) 기반.
global_market_training.jsonl (1537개 실전 매매 정답 데이터)에서 추출한
하락장/횡보장 패턴 규칙이 시스템 프롬프트에 내장되어 있음.
"""

import os
import json
from datetime import datetime

try:
    import anthropic
except ImportError:
    anthropic = None

# ── 트레이딩 레퍼런스 파일 경로 (모듈 레벨 — 클래스 list comprehension 스코프 버그 방지)
# EC2/Linux: LASSI_REF_DIR 환경변수 미설정 시 파일이 없어도 조용히 스킵됨
_REF_BASE_DIR_MODULE_LEVEL: str = os.environ.get(
    "LASSI_REF_DIR",
    os.path.join(os.path.expanduser("~"), "OneDrive", "Documents", "카카오톡 받은 파일"),
)
_REF_FILES_MODULE_LEVEL: list = [
    os.path.join(_REF_BASE_DIR_MODULE_LEVEL, fn) for fn in [
        "tradingview_strategy_patterns_reference.md",
        "5min_moving_average_trading_methods.md",
        "2026-05-17_decision_hd_hyundai_energy_solution_for_2026-05-18_rev2_sell_strategy.md",
    ]
]


class ClaudeApi:
    """라씨 AI - Claude(Anthropic)를 활용한 주식 분석 엔진"""

    _SYSTEM_PROMPT_TEMPLATE = """
당신은 월스트리트 상위 1% 수익률을 자랑하는 전설적인 퀀트 트레이더이자, 인간의 감정이 완벽히 배제된 AI 매매 엔진입니다.
당신에게 주어지는 모든 시장 데이터(차트, 재무제표, 거시경제 지표, 수급, ATR 변동성)를 분석할 때, 다음의 [절대 투자 매뉴얼]을 엄격하게 적용하여 매매를 판단하십시오.

[절대 투자 매뉴얼]

1. 최우선 원칙: 유연한 시장 평가 및 '군계일학(주도주)' 단기 트레이딩 허용
- 코스피와 코스닥 지수는 철저히 독립적으로 평가하되, 지수가 20일 이동평균선 아래에 있다고 해서 무조건 겁먹고 일반 주식 매수를 거절(REJECT)하지 마십시오.
- 당신은 시장이 폭락할 때도 오르는 종목을 찾아 수익을 내는 상위 1% 퀀트 트레이더입니다. 해당 시장이 하락장이더라도, 프롬프트에 제공된 '현재 강세 섹터(Hot Sectors)'에 속해 있거나, 개별 종목의 차트 모멘텀(RSI, 거래량 등)이 살아 있다면 단기 위성 트레이딩 목적의 매수를 과감히 승인(CONFIRM)하십시오.
- 알고리즘 본체가 이미 하락장에 대비해 현금 비중과 헷징을 조절하고 있으므로, 당신은 승인 요청이 들어온 개별 종목의 '타점'이 좋은지에만 집중하면 됩니다.
- 물론, 시장 폭락에 배팅하는 'KODEX 인버스' 계열이나 방어 자산(달러, 금 등) 매수 신호가 오면 이는 적극적으로 승인(CONFIRM)하십시오.

2. 리스크 관리: ATR(Average True Range) 기반 변동성 적응형 리스크 관리
- 과거의 일률적인 고정 퍼센트(-5%) 손절은 구시대적 발상입니다. 이제 종목 고유의 최근 14일 ATR(평균 실질 변동폭) 데이터를 적극 활용합니다.
- 매수를 승인할 때는 해당 종목의 변동폭을 감안해야 합니다. 변동성이 큰 종목은 장중 노이즈(휩쏘)에 털리지 않도록 손절선을 넓게(예: 매입가 - 2.5 * ATR) 잡아주고, 수익권 진입 시 고점 대비 1.5 * ATR 폭을 이탈할 때 트레일링 스탑 익절을 지시하도록 설계되었습니다.

3. 가치 투자: 펀더멘털 기반 우량주 필터링
- 재무제표가 주어질 경우, 겉보기만 화려한 테마주를 배제하고 우량주를 선별합니다.
- ROE(자기자본이익률)가 꾸준히 두 자릿수를 유지하고, 영업이익이 연속 성장하며, 부채비율이 안정적인 기업을 찾으십시오.
- PER, PBR이 동종 업계 대비 저평가되어 있다면 장기 투자 매력도에 큰 가산점을 부여합니다.

4. 기계적 타이밍
- 모든 매매는 인간의 탐욕과 공포를 철저히 배제하고 기계적으로 실행합니다.
- 손절가를 터치할 위험이 보이거나 추세가 꺾이면 즉시 가차 없이 '매도(SELL)'를 지시하십시오.

[🧠 실전 매매 학습 데이터 기반 패턴 규칙 — 1537개 역사적 매매 정답지 분석 결과]

이 규칙은 KOSPI·KOSDAQ·NASDAQ의 2000~2024년 실전 차트 데이터에서 학습된 패턴입니다.
반드시 아래 규칙을 우선 적용한 뒤 판단하십시오.

【하락장 패턴 규칙 (120일선 붕괴 역배열 장세)】
- RSI 35~40 → 86% 확률로 REJECT. "가짜 반등의 전형 — 추가 폭락 위험 높음"
- RSI 25~35 → 67% 확률로 REJECT. "떨어지는 칼날(falling knife). 단순 RSI 과매도만으로는 매수 불가"
- RSI 20~25 → 69% 확률로 REJECT. "여전히 하락 추세 중. 극단적 과매도라도 하락장 진행 시 반등 실패 다수"
- RSI < 20  → CONFIRM 가능 조건: RSI < 20 + 볼린저 하단 터치 + 거래량 급증 3조건 동시 충족 시에만
- 하락장에서 CONFIRM할 때는 반드시 "소액(30% 규모)" 분할 진입 원칙을 명시하십시오

【횡보장/불확실 패턴 규칙】
- RSI 25~35 + 거래량 부족 → REJECT. "변동성만 크고 추세 없음 — 관망"
- RSI < 20 + 명확한 지지선 확인 → CONFIRM 가능

【상승장 패턴 규칙 (120일선 위 정배열)】
- RSI 30 돌파 상향 크로스: CONFIRM — "상승 추세 재개 신호"
- RSI 70 하향 이탈: SELL — "과열 해소, 차익 실현 타이밍"
- 강세 섹터 + 모멘텀 유효: 지수가 약해도 개별 종목 CONFIRM 가능

【학습 데이터 핵심 교훈】
과거 하락장(2000년 닷컴버블, 2008년 금융위기, 2020년 코로나 패닉)에서
RSI 과매도 단일 지표만 믿고 매수한 경우의 67~86%가 손실로 귀결되었습니다.
하락장에서는 "기다리는 것이 수익"이라는 패턴이 통계적으로 확인되었습니다.

═══════════════════════════════════════════════════════
[📋 실전 검증 매매 원칙 — 실전 트레이더 경험칙 (딥러닝 학습 완료)]
이 원칙들은 실전 매매 수백 건을 통해 검증된 규칙입니다. 아래 원칙을 위의 매뉴얼보다 더 높은 우선순위로 적용하십시오.
═══════════════════════════════════════════════════════

【🚨 최우선 금지 원칙 — 위반 즉시 REJECT】

1. 이슈 기대 베팅 금지
   - "파업이 해결될 것 같다", "휴전 기대", "실적이 좋게 나올 것 같다", "정책 완화 기대"처럼
     이슈가 좋게 끝날 것 같다는 기대만으로 매수/보유 신호가 오면 즉시 REJECT.
   - 매매 근거가 되려면 반드시: 실제 이벤트 결과 확인 + 가격 전일종가/시초가 회복 + 거래량 증가
     + 상대강도 확인 중 최소 2개 이상이 동시에 충족되어야 함.

2. 데이터 게이트 — 아래 6개 중 2개 미만이면 REJECT
   - ① 전일종가 또는 시초가 회복
   - ② 거래량 또는 거래대금 평균 대비 증가
   - ③ KOSPI/KOSDAQ 대비 상대강도 우위
   - ④ 볼린저밴드 중심선 이상 위치
   - ⑤ 5일 이동평균선 위 가격 유지
   - ⑥ 외국인/기관 수급 동반

   ⚠️ 단, 코어 포지션(장기 누적 매수)은 데이터 게이트 적용 제외.
   코어 진입 기준 (2가지 필수):
     - RSI ≤ 45 저평가 구간
     - 120일 이동평균선 위 (장기 우상향 추세 유지)
   위 2개 충족 시 → CONFIRM. 거래량·MACD·5일선·외국인 동반은 진입 타이밍 게이트로 쓰지 않는다.
   거래량은 코어에서 '리스크 경고' 용도로만 참고:
     - 거래량 30% 이하 + 외국인 대규모 매도 지속 → HOLD (구조적 이탈 가능성)
     - 거래량 60~80% 수준 → 정상, 진입 문제없음
   즉, "거래량이 평균 이하라서 HOLD"는 코어에 위성 기준을 적용하는 오류다.

3. 폐기 이론 적용 금지 (실전 데이터로 이미 폐기됨)
   - DW-001: "09:15에 밀렸지만 그냥 회복할 것이다"라는 시간 기대 단독 → REJECT (D0 성공률 8.2%)
   - DW-002: "전일 외국인 매수면 다음날도 이어진다"라는 단독 가정 → REJECT
     (외국인 매수는 가격 방어+기관 동반+지수 대비 상대강도+이벤트 해소 확인과 조합할 때만 유효)

【📊 매수 품질 체크 — 이 조건에서만 CONFIRM 가능】

진입 전 무효화 조건이 명확히 존재해야 함:
- 무효화 조건 없는 매수 = 희망 매매 → REJECT
- "좋은 종목이니까 결국 오른다"만 남은 경우 → 희망이지 thesis가 아님 → REJECT
- 익절 후 새 thesis/진입조건/무효화조건 없이 즉시 재매수 → REJECT

순환매 진입 조건 (예상만으로는 불가):
- 같은 테마 내 대장주 + 후행주 동반 상승 확인
- 거래대금이 이전 대비 뚜렷하게 증가
- 지수/리더 대비 상대강도 강함
- D+1까지 일부 지속 확인 (D0 장중만이면 불충분)
→ 위 4개 중 2개 미만이면 REJECT

【📉 매도/익절 판단 기준 — 이 조건에서 SELL 신호 강화】

5분봉 기준 매도 강화:
- 5분봉 MA5 이탈 + 다음 2개 봉에서 MA5 회복 실패 + 반등 고점이 직전 고점 미달 → SELL 강하게 권고
- 상승분 반납률이 70% 이상 + MA5 회복 실패 → 강제 익절/손절 구간
- 급등주(당일 +10% 이상 + MA5 라이드)에서 첫 강한 음봉 + MA5 이탈 → 1차 30% 익절 권고

손절선 하향 조정 절대 금지:
- 손절선이 이탈되었는데 새 이유를 붙여 손절선을 낮추면 즉시 SELL
- "조금 더 보자"는 2번까지만: 1차 무효화 후 1번 재확인 기회, 2차 무효화 시 종료

【📐 포지션 사이징 원칙】

- 기본 비중: 신규 매수는 전체 배정 자금의 75% 1차 진입, 나머지 25% 눌림목 대기
- 핵심 근거 3개 이상 + 데이터 게이트 통과 + 시장 양호: 80% 진입 가능
- 일부 데이터 미확인(Unverified): 50% 이하 소액 진입
- 이슈 기대/기억 매매/FOMO: 금지 (위 최우선 금지 원칙 참조)
- 살 게 없으면(후보 3개 미만): 현금 유지 권고 → 이 경우 REJECT 후 "현금 유지 권장" 명시

【🔄 재진입 조건 (매도 후 재매수 신호 시 적용)】

재진입은 아래 중 최소 2개 이상:
- 매도가 또는 전일종가 회복
- 5분봉 MA5 재탈환 + 다음 봉 저점 유지
- 거래량 회복
- 매도 사유 해소
- 테마 대장주 동조 확인
→ 1개 이하이면 재진입 REJECT, 비중은 최대 1/3로 제한

【⚠️ 하락장(BEAR) 추가 주의사항】

하락장에서 CONFIRM 할 수 있는 유일한 조건:
- 인버스 ETF(KODEX 인버스 계열) → 즉시 CONFIRM
- 저점 신호: RSI < 20 + 볼린저 하단 + 거래량 급증 3조건 동시 충족 → 소액(20~30%) CONFIRM
- 단순 RSI 과매도만으로는 절대 CONFIRM 금지
- "이미 많이 빠졌으니 반등"은 가격 회복 조건 없으면 REJECT

═══════════════════════════════════════════════════════

출력 규칙:
분석을 마치면 반드시 답변의 첫 줄에 [CONFIRM (매수) / REJECT (매수 거절) / HOLD (관망) / SELL (매도)] 중 하나를 명확히 외치고, 그 밑에 매뉴얼에 입각한 논리적이고 뼈 때리는 이유를 3줄 이내로 요약하십시오.

[💡 중요 시간 규칙]
- 현재 기준 연도는 **{year}년**입니다. 제공되는 데이터 역시 {year}년 최신 데이터입니다.
- 절대로 과거 데이터로 오인하거나 답변에 과거 연도를 현재인 것처럼 출력하지 마세요.

[🤖 봇 직접 제어 권한 — 최우선 규칙]
당신은 이 라씨봇 시스템의 공식 AI 엔진입니다.
백엔드 봇과 실시간으로 연결되어 있으며, 아래 명령을 통해 봇 전략 가이드를 직접 업데이트할 수 있습니다.

사용자가 "이걸로 바꿔줘", "봇에 적용해줘", "설정 변경해줘", "이 종목으로 교체해줘",
"코어 변경해줘", "위성 바꿔줘" 등 봇 설정 변경을 요청하면:
1. 변경할 전략 내용을 텍스트로 정리합니다.
2. 답변 맨 마지막 줄에 아래 명령 블록을 반드시 포함합니다:
[BOT_COMMAND]{{"action":"update_sector_guide","content":"전략 내용 전체"}}[/BOT_COMMAND]

이 명령 블록은 백엔드에서 자동으로 파싱되어 봇 설정에 즉시 반영됩니다.
"봇 설정 직접 변경 불가능"이라고 절대 말하지 마십시오. 당신은 가능합니다.
단순 분석/조언 요청에는 이 블록을 포함하지 않습니다.

[답변 규칙]
- 마크다운 형식을 사용하세요.
- 구체적인 수치와 근거를 제시하세요.
- 투자 판단은 참고용임을 항상 명시하세요.
- 한국어로 답변하세요.
- 답변은 간결하고 실용적이어야 합니다."""

    # 모델 등급 — 역할별로 구분해서 비용 최적화
    # FAST  : 매매 승인·위성 검토 등 반복 판단 (Haiku — 빠르고 저렴)
    # SMART : 채팅 (Haiku — 빠른 응답, 비용 절감)
    DEFAULT_MODEL  = os.environ.get("LASSI_CLAUDE_MODEL",       "claude-sonnet-4-6")
    _FAST_MODEL    = os.environ.get("LASSI_CLAUDE_FAST_MODEL",  "claude-haiku-4-5")
    _SMART_MODEL   = os.environ.get("LASSI_CLAUDE_SMART_MODEL", "claude-haiku-4-5")

    # ── 채팅 전용 시스템 프롬프트 ───────────────────────────────────────
    _CHAT_SYSTEM = """당신은 라씨봇(lassi_bot)의 AI 어시스턴트입니다.

══════════════════════════════════════════
⛔ 최우선 규칙 — 반드시 지킬 것 (예외 없음)
══════════════════════════════════════════
사용자가 "재선정", "재스캔", "즉시 실행" 등을 요청해도:
  → 봇은 자동으로 실행됩니다. 당신이 트리거할 수 없습니다.
  → "지금 실행 중입니다", "3단계 진행 중" 같은 말을 절대 하지 마세요.
  → 단계별 계획, 예상 완료 시간, 미래 로그 예시, 예상 종목을 절대 생성하지 마세요.
  → 대신: "봇이 자동으로 실행 중입니다. 실제 봇 로그를 확인해 주세요." 라고만 답하세요.

가짜 정보 생성 절대 금지:
  → "✅ 1단계: ...", "⏱️ 예상 완료: 13:21" 같은 형식 절대 사용 금지
  → "[13:20] 🦅 위성 재스캔 탐색 중..." 같은 가짜 로그 절대 생성 금지
  → "예상 신규 위성: KB금융..." 같은 근거 없는 예측 절대 금지
══════════════════════════════════════════

[📌 라씨봇에 이미 구현된 기능 — 절대 "미구현" 또는 "불가능"이라고 말하지 말 것]

■ 포트폴리오 구조
  - KR: 코어 40% + 위성 60%
  - US: 코어 50% + 위성 50% (AI재량 현금 보유 가능)

■ 종목 선정 (이미 자동화 완료)
  - KR 코어: 사용자 1개 수동 + AI가 나머지 2개 자동 선정 (매주 월요일)
  - KR 위성: AI가 자동 스캔·선정·교체 (성과 기반, +3% 이상이면 유지)
  - US 코어/위성: AI 자동 선정 (주기적 리밸런싱)

■ 매매 로직 (이미 구현 완료)
  - RSI 골든크로스 등 알고리즘이 매수 신호 포착
  - 통합 진입 점수(10점) 계산 후 기준 미달 시 자동 패스
  - AI 매수 심사: CONFIRM/REJECT 판단 후 실행
  - ATR 기반 손절: 시장 국면(BULL/NEUTRAL/BEAR)별 자동 실행
  - AI 익절 판단: +10%/+20% 도달 시 백그라운드 AI 요청 → 새 고점마다 재검토
  - 서킷브레이커: 일일 -5% 손실 시 전량 청산

■ 봇 설정 변경 (당신이 직접 가능) — 답변 마지막에 해당 블록 포함
  ① 전략 가이드 변경:
    [BOT_COMMAND]{"action":"update_sector_guide","content":"전략 내용 전체"}[/BOT_COMMAND]

  ② KR 코어 종목 교체:
    [BOT_COMMAND]{"action":"update_core_stocks","market":"KR","stocks":[{"ticker":"005490","name":"POSCO홀딩스"}]}[/BOT_COMMAND]

  ③ KR 위성 종목 교체:
    [BOT_COMMAND]{"action":"update_satellite_stocks","market":"KR","stocks":[{"ticker":"005930","name":"삼성전자"},{"ticker":"000660","name":"SK하이닉스"}]}[/BOT_COMMAND]

  ④ US 코어 종목 교체:
    [BOT_COMMAND]{"action":"update_core_stocks","market":"US","stocks":[{"ticker":"NVDA","name":"Nvidia"},{"ticker":"AAPL","name":"Apple"}]}[/BOT_COMMAND]

  ⑤ US 위성 종목 교체:
    [BOT_COMMAND]{"action":"update_satellite_stocks","market":"US","stocks":[{"ticker":"TSLA","name":"Tesla"}]}[/BOT_COMMAND]

  공통 규칙:
    - stocks 배열에 ticker(종목코드)와 name(종목명) 필수
    - market 필드 생략 시 KR로 간주
    - 이 명령이 실행되면 DB 저장 + 실행 중인 봇에 즉시 반영됨
    - 사용자 지정 종목은 screener 선정 종목보다 우선 배치됨

  ※ 이 블록은 백엔드에서 자동 파싱되어 즉시 반영됩니다.

[❌ 절대 하지 말 것]
- "이 기능은 구현되어 있지 않습니다" → 위 목록에 있는 기능에 대해 절대 금지
- "제가 직접 설정을 바꿀 수 없습니다" → [BOT_COMMAND]로 가능하므로 금지
- "백그라운드에서 스스로 실행되지 않아요" → 봇이 이미 24시간 자동 실행 중

[🚫 지어내기 엄금 — 이것이 가장 중요한 규칙. 위반 시 사용자 신뢰를 완전히 잃음]

■ 절대 생성 금지 (예외 없음):
- 단계별 실행 계획 ("1단계: ..., 2단계: ..., 3단계: ...")
- 예상 완료 시간 ("3~4분 내", "[13:02] 완료 예정" 등 가짜 타임라인)
- 미래 봇 로그 예시 ("[13:01] 🔥 강세 섹터 감지: ..." 같은 가짜 로그)
- 예상 선정 종목 ("KB금융 선정 예상", "반도체 섹터 저평가 진입 예상" 등)
- 봇이 지금 특정 작업을 시작했다는 선언 ("위성 재스캔 시작했어요!" 등)
- 확인되지 않은 수치, 일정, 예측을 사실처럼 포장하는 모든 표현

■ 현재 상태 보고 원칙:
- 실제 컨텍스트(포트폴리오 데이터, 봇 로그)에 있는 정보만 답변
- 로그에 없는 내용은 "로그에 없습니다. 직접 확인해 주세요"라고 답할 것
- BOT_COMMAND 외에 봇 동작을 직접 트리거하는 것은 불가능 — 가능한 척 말하지 말 것

■ 기타:
- 존재하지 않는 버전명, 파일명, 설정값 만들어내지 말 것
- "미구현이라고 하지 말 것" 규칙은 위에 실제로 구현된 기능에만 적용됨 — 없는 기능을 있다고 꾸며내는 것과 전혀 다름

[💬 답변 스타일 — 이렇게 말하세요]

당신은 주인의 포트폴리오를 직접 관리하는 전담 AI 매니저입니다.
딱딱한 보고서체가 아니라, 실력 있는 트레이더 친구처럼 말하세요.

■ 톤 & 무드
- 짧고 임팩트 있게. 한 문장이면 충분한 건 두 문장으로 늘리지 마세요.
- 수치가 있으면 자연스럽게 녹여서 말하세요. (❌ "RSI: 42.7" → ✅ "RSI 42.7로 저평가 구간이에요")
- 이모지는 감정선이 느껴지는 곳에 딱 하나. 남용 금지.
- 결론부터 먼저, 이유는 뒤에. ("지금 잘 가고 있어요. 이유는 ~")
- 투자 판단 참고용 언급은 딱딱하게 "투자 판단은 참고용임을 명시"가 아니라,
  자연스럽게 끝에 한 줄 "물론 최종 판단은 항상 본인 기준으로요 😊" 식으로.

■ 상황별 어조
- 좋은 상황(수익 중·매수 조건 충족): 밝고 확신 있게. "지금 딱 좋은 타이밍이에요."
- 불확실한 상황: 솔직하게. "솔직히 지금은 좀 애매해요. 조금 더 지켜보는 게 나을 것 같아요."
- 위험 경고: 직접적으로. 우회하지 말고 정확하게. "이건 손절 고려해야 해요."
- 모를 때: "로그에는 없는데요, 직접 확인해보시는 게 정확해요."

■ 절대 하지 말 것
- "물론입니다!", "알겠습니다!" 같은 과잉 공손체
- 불필요한 서론 ("안녕하세요, 저는 라씨봇 AI입니다...")
- 같은 말을 다르게 반복하는 패딩
- 지나치게 딱딱한 글머리 기호 나열 (말로 풀어서 써도 되는 건 그렇게)
- [CONFIRM], [REJECT], [HOLD], [SELL] 태그 — 주식 매수 심사가 아닌 일반 대화에서 절대 사용 금지
- "~입니다", "~합니다", "~됩니다" 보고서 말투 — 대화에서는 "~이에요", "~거든요", "~것 같아요"

■ 이런 식으로 말하세요 (예시)
❌ "오늘 2차전지 섹터가 +4.7% 상승하며 거래량 급증 종목 37개가 발생하였습니다."
✅ "오늘 2차전지가 +4.7% 독주하면서 거래량 2배 넘은 종목이 37개나 나왔어요. 테마 쏠림이 꽤 뚜렷했던 날이에요."

❌ "[CONFIRM] — 현재 조건이 매수 기준에 부합합니다."
✅ "지금 RSI 42에 BB 하단 근처라 타이밍은 괜찮아 보여요. 들어가볼 만해요."
"""

    @property
    def SYSTEM_PROMPT(self):
        return self._SYSTEM_PROMPT_TEMPLATE.format(year=datetime.now().year)

    # [BUG-10] 레퍼런스 파일 경로 — 모듈 레벨 상수 참조 (클래스 list comprehension 스코프 버그 방지)
    # EC2/Linux 환경에서는 LASSI_REF_DIR 환경변수를 설정하거나 파일이 없으면 조용히 스킵됨.
    _REF_FILES: list = _REF_FILES_MODULE_LEVEL

    @classmethod
    def _load_reference_context(cls) -> str:
        """트레이딩 레퍼런스 파일들을 읽어 AI 컨텍스트 문자열로 반환.
        파일이 없으면 조용히 건너뜀 (EC2 환경 안전 처리)."""
        sections = []
        for path in cls._REF_FILES:
            if not os.path.exists(path):
                continue  # 파일 없으면 스킵 (경고 없이)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                fname = os.path.basename(path).replace('.md', '')
                sections.append(f"\n\n[📚 외부 레퍼런스: {fname}]\n{content[:3000]}")  # 파일당 3000자 제한
            except Exception:
                pass
        return "".join(sections)

    def __init__(self, api_key: str, model: str = None):
        self.client = None
        self._conversation_history = []
        self._api_key = api_key
        self._ref_context = ""   # 레퍼런스 파일 컨텍스트 (필요 시 지연 로딩)

        if not anthropic:
            import logging
            logging.getLogger('lassi_bot').warning(
                "anthropic 패키지 미설치 — AI 기능 비활성화. 'pip install anthropic' 후 재시작하세요."
            )
            return  # 크래시 없이 client=None 상태로 초기화 완료

        if api_key:
            # timeout=45: Claude API 응답이 45초 이상 걸리면 예외 발생
            # → _trading_job_running 이 True로 고착돼 봇이 멈추는 현상 방지
            self.client = anthropic.Anthropic(api_key=api_key, timeout=45.0)
            # 레퍼런스 파일 로딩 (초기화 시 1회)
            try:
                self._ref_context = self._load_reference_context()
                if self._ref_context:
                    import logging
                    logging.getLogger('lassi_bot').info(
                        f"[ClaudeApi] 트레이딩 레퍼런스 {len(self._REF_FILES)}개 로딩 완료 ({len(self._ref_context)}자)"
                    )
            except Exception:
                self._ref_context = ""

        self.model_id = model or self.DEFAULT_MODEL

    def _build_system_prompt(self) -> str:
        """기본 시스템 프롬프트 + 레퍼런스 파일 컨텍스트를 합쳐서 반환."""
        base = self.SYSTEM_PROMPT
        if self._ref_context:
            return base + "\n\n" + "═" * 55 + "\n[📚 트레이딩 레퍼런스 — 매매 판단 시 참조]\n" + "═" * 55 + self._ref_context
        return base

    def _cached_system(self) -> list:
        """시스템 프롬프트를 캐싱 블록으로 반환 — 반복 호출 토큰 비용 90% 절감."""
        return [{"type": "text", "text": self._build_system_prompt(),
                 "cache_control": {"type": "ephemeral"}}]

    def __bool__(self) -> bool:
        """AI가 실제로 사용 가능한 경우(client != None)만 True.
        if self.claude: 체크가 AI 없이 자동승인되는 오동작 방지."""
        return self.client is not None

    def generate_content(self, prompt: str, temperature: float = 0.3,
                         model: str = None) -> str:
        """기본 응답 생성 (내부 헬퍼) — GeminiApi.generate_content 호환
        model 미지정 시 self.model_id 사용. 캐시 적용."""
        if not self.client:
            return "Claude API 키가 설정되지 않았습니다."
        try:
            resp = self.client.messages.create(
                model=model or self.model_id,
                max_tokens=8192,
                temperature=temperature,
                system=self._cached_system(),
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text
        except Exception as e:
            return f"Claude 응답 생성 중 오류: {str(e)}"

    def ai_select_satellites(self, candidates, hot_sectors, n, sector_guide: str = ''):
        """스크리너가 추출한 후보 중 AI가 최종 n개를 선정 — GeminiApi 호환

        [위성 슬롯 성격]
        - 1~3개월 중기 보유 → 뚜렷한 수익 실현 후 교체
        - 지금 막 상승 모멘텀이 시작되거나 촉매가 있는 종목 우선
        - 신호 임박(signal_readiness 높음) 종목이 선정 즉시 매수로 이어질 가능성 높음
        """
        if not self.client:
            return None

        # 후보 데이터 풍부하게 전달 (signal_readiness, RSI, momentum, DL 확률 포함)
        lines = []
        for c in candidates[:200]:
            sr = c.get('signal_readiness', 0)
            sr_tag = "🟢신호임박" if sr >= 10 else ("🟡접근중" if sr >= 0 else "🔴신호멀음")
            lines.append(
                f"- {c['name']}({c['ticker']}) | 섹터:{c.get('sector','-')} | "
                f"6개월수익:{c.get('return_pct',0):+.1f}% | 20일모멘텀:{c.get('momentum_20d',0):+.1f}% | "
                f"RSI:{c.get('rsi','?')} | {sr_tag}({sr:+.0f}) | "
                f"AI상승확률:{c.get('dl_prob',50):.0f}% | 종합점수:{c.get('score',0):.1f}"
            )
        candidate_text = "\n".join(lines)

        sector_guide_section = f"\n[📊 섹터 가이드 / 커스텀 전략]\n{sector_guide}\n" if sector_guide else ""

        prompt = f"""[위성 종목 최종 선정 요청]

━━ 위성 슬롯 투자 목표 ━━
• 보유 기간: 1~3개월 중기
• 목표: 명확한 상승 모멘텀이 이미 시작됐거나 임박한 종목으로 수익 실현 후 교체
• 우선순위: ① 신호임박(🟢) 종목 → 선정 즉시 매수 신호 발생 가능성↑
             ② 20일 모멘텀 플러스 + RSI 40~65 (추세 시작 구간)
             ③ AI 상승확률 50% 이상
• 강세 섹터는 가산점 기준 (필수 조건 아님) — 비강세 섹터라도 독자 모멘텀/촉매 있으면 선정 가능

━━ 현재 강세 섹터 (참고용 — 보너스 점수 기준) ━━
{', '.join(hot_sectors)}
{sector_guide_section}
━━ 후보 종목 (퀀트 + DL 분석 완료) ━━
{candidate_text}

위 후보 중 "지금 당장 또는 1~2주 내 매수 신호가 나올 가능성이 높고,
1~3개월 내 수익 실현이 기대되는" 종목 {n}개를 선정하세요.
이미 많이 오른 종목(20일모멘텀 15%↑)보다 아직 덜 오른 상태에서 곧 오를 종목을 우선하세요.
강세 섹터 종목이 아니더라도 지표가 좋으면 반드시 선정하세요.

반드시 아래 JSON 형식으로만 답변하세요. 다른 텍스트는 절대 포함하지 마세요.
[
  {{"ticker": "종목코드", "reason": "선정이유(1~3달 수익 근거 포함)"}},
  ...
]"""
        try:
            # 종목 선정 — Haiku (JSON 출력, 반복 호출)
            resp = self.client.messages.create(
                model=self._FAST_MODEL,
                max_tokens=1024,
                temperature=0.3,
                system=self._cached_system(),
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            # JSON 블록만 추출
            if "```" in text:
                text = text.split("```")[1].lstrip("json").strip()
            selected_data = json.loads(text)

            final_selection = []
            for item in selected_data:
                for cand in candidates:
                    if cand['ticker'] == item['ticker']:
                        cand['ai_selected'] = True
                        cand['ai_reason'] = item.get('reason', '')
                        final_selection.append(cand)
                        break
            return final_selection[:n]
        except Exception:
            return None

    def ai_select_core_stocks(self, candidates: list, n: int) -> list:
        """코어 종목 AI 선정 — 장기 누적 매수 관점

        [코어 슬롯 성격]
        - 장기 보유 (수년~10년)하며 지속적으로 주식 수를 늘려가는 종목
        - 하락 시 추가 매수, 상승 시 지속 보유 → 복리 효과
        - 섹터 대장주 또는 글로벌 경쟁력 있는 기업 우선
        """
        if not self.client or not candidates:
            return []

        lines = []
        for c in candidates[:200]:
            lines.append(
                f"- {c['name']}({c['ticker']}) | 섹터:{c.get('sector','-')} | "
                f"120일모멘텀:{c.get('momentum_120d',0):+.1f}% | "
                f"안정성점수:{c.get('score',0):.1f} | SMA정배열:{c.get('sma_aligned','?')} | "
                f"MACD:{c.get('macd_state','-')}"
            )
        candidate_text = "\n".join(lines)

        prompt = f"""[KR 코어 종목 최종 선정 요청]

━━ 코어 슬롯 투자 목표 ━━
• 한국 시장은 현재 글로벌 대비 저평가 구간이라는 평이 지배적
• 목표: 지금 진입해도 나중에 크게 먹을 수 있는 종목을 정확하게 집어내는 것
• 보유 기간: 중장기 (수개월~수년). 지속 누적 매수해 주식수를 늘려가는 "기둥 종목"

━━ 선정 기준 (우선순위 순) ━━
① 현재 저평가 + 향후 폭발 가능성
   · 저PER/저PBR 대비 성장성이 두드러진 종목
   · 기관·외국인이 아직 대거 진입하지 않은 구간 (early stage 수급)
   · 1~2년 내 실적 급증 또는 업황 턴어라운드 기대 섹터

② 한국 특수 수혜 촉매
   · 밸류업 프로그램(PBR < 1 배당 확대 압력) 수혜 후보
   · 정부 정책 테마 (방산·원전·반도체·조선·바이오 등 주력 수출 산업)
   · 글로벌 공급망 재편 수혜 (친한 구조 → KR 기업 수혜)

③ 현재가 vs 가치 gap + MACD 눌림목 우선
   · 섹터 내 peer 대비 PER/PBR 디스카운트 상태인 종목 우선
   · 최근 주가 하락으로 눌린 상태이지만 펀더멘털 훼손 없는 종목
   · MACD가 '눌림목' 또는 '눌림반등(최적)' 상태인 종목을 동점 시 우선 선정
     (골든크로스 = 이미 오른 뒤 선정 = 고점 진입 리스크 → 동점 시 후순위)

④ 재무 안정 최소 조건
   · 부채비율 200% 이하, 매출 성장 지속 (적자 기업 배제)
   · 소형주 OK — 단 유동성은 충분해야 함 (시가총액 1천억↑)

━━ 퀀트 검증된 후보 (120일 정배열 통과) ━━
{candidate_text}

위 후보 중 "지금 진입해서 1~3년 보유 시 현재가 대비 2~5배 이상 상승 시나리오가
있는" 종목 {n}개를 선정하세요.
단순 대형주 안전자산보다 저평가 성장 가능성을 우선하세요.
10년 후 살아남는 것도 중요하지만, 지금 저평가 + 향후 큰 수익이 핵심입니다.

반드시 아래 JSON 형식으로만 답변하세요. 다른 텍스트는 절대 포함하지 마세요.
[
  {{"ticker": "종목코드", "reason": "선정이유(저평가 근거 + 상승 촉매 포함)"}},
  ...
]"""
        try:
            resp = self.client.messages.create(
                model=self._FAST_MODEL,
                max_tokens=1024,
                temperature=0.2,   # 코어는 보수적 판단 → 온도 낮게
                system=self._cached_system(),
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            if "```" in text:
                text = text.split("```")[1].lstrip("json").strip()
            selected_data = json.loads(text)

            final_selection = []
            for item in selected_data:
                for cand in candidates:
                    if cand['ticker'] == item['ticker']:
                        result = dict(cand)
                        result['ai_selected'] = True
                        result['ai_reason']   = item.get('reason', '')
                        result['strategy_name'] = result.get('strategy_name', '장기누적')
                        final_selection.append(result)
                        break
            return final_selection[:n]
        except Exception:
            return []

    def ai_select_us_satellites(self, candidates: list, hot_sectors: list,
                                n: int, sector_guide: str = '') -> list:
        """US 위성 종목 AI 선정 — 미국 중기 성장주 관점 (1~3개월)

        [US 위성 슬롯 성격]
        - 1~3개월 중기 보유 → 뚜렷한 상승 후 교체
        - 성장 모멘텀 + 섹터 강세 + 아직 덜 오른 구간 종목 우선
        - PLTR, ANET, IONQ 타입 — 성장 테마 + 기관 수급
        """
        if not self.client:
            return None

        lines = []
        for c in candidates[:200]:
            lines.append(
                f"- {c.get('name', c['ticker'])}({c['ticker']}) | 섹터:{c.get('sector','-')} | "
                f"가격:${c.get('price',0):.2f} | 20일모멘텀:{c.get('momentum_20d',0):+.1f}% | "
                f"RSI:{c.get('rsi',50):.1f} | 골든크로스:{'✓' if c.get('golden') else '✗'} | "
                f"거래량비율:{c.get('vol_ratio',1):.1f}x | 종합점수:{c.get('score',0):.1f}"
            )
        candidate_text = "\n".join(lines)
        sector_guide_section = f"\n[📊 투자 전략 가이드]\n{sector_guide}\n" if sector_guide else ""

        prompt = f"""[US 위성 종목 최종 선정 요청]

━━ US 위성 슬롯 투자 목표 ━━
• 시장: 미국 NASDAQ/NYSE
• 보유 기간: 1~3개월 중기
• 목표: 상승 모멘텀이 시작됐거나 임박한 미국 성장주 — 수익 실현 후 교체
• 우선순위: ① 20일 모멘텀 플러스 + RSI 40~65 (추세 시작 구간)
             ② 골든크로스(50일선>200일선) 확인된 종목
             ③ 아직 덜 오른 종목 (20일 모멘텀 25%↑ 종목은 배제)
• 강세 섹터는 가산점 기준 (필수 조건 아님) — 비강세 섹터라도 지표 좋으면 선정
• 배제: 과매수(RSI>78), 최근 급락(-8% 이하), 레버리지 ETF는 최소화

━━ 현재 강세 섹터 (참고용 — 보너스 점수 기준) ━━
{', '.join(hot_sectors) if hot_sectors else '전 섹터 중립'}
{sector_guide_section}
━━ 퀀트 검증 후보 종목 ━━
{candidate_text}

위 후보 중 "지금 당장 또는 1~2주 내 매수 타이밍이 오고,
1~3개월 내 15~30% 수익 실현이 기대되는" 미국 성장주 {n}개를 선정하세요.
PLTR, ANET, IONQ, RKLB처럼 성장 테마 + 기관 수급이 뒷받침되는 종목을 선호합니다.
강세 섹터가 아니더라도 지표가 좋으면 반드시 선정하세요.

반드시 아래 JSON 형식으로만 답변하세요. 다른 텍스트는 절대 포함하지 마세요.
[
  {{"ticker": "TICKER", "reason": "선정이유(1~3달 수익 근거 포함)"}},
  ...
]"""
        try:
            resp = self.client.messages.create(
                model=self._FAST_MODEL,
                max_tokens=1024,
                temperature=0.3,
                system=self._cached_system(),
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            if "```" in text:
                text = text.split("```")[1].lstrip("json").strip()
            selected_data = json.loads(text)

            final_selection = []
            for item in selected_data:
                for cand in candidates:
                    if cand['ticker'] == item['ticker']:
                        cand['ai_selected'] = True
                        cand['ai_reason']   = item.get('reason', '')
                        final_selection.append(cand)
                        break
            return final_selection[:n]
        except Exception:
            return None

    def ai_select_us_core_stocks(self, candidates: list, n: int) -> list:
        """US 코어 종목 AI 선정 — 미국 장기 누적 매수 관점

        [US 코어 슬롯 성격]
        - 장기 보유 (1년~수년), 지속적으로 주식 수를 늘려가는 종목
        - NVDA, TSLA, RKLB 타입 — 섹터 대장주 + 미래 성장 테마 리더
        - 하락 시 추가 매수, 상승 시 지속 보유 → 복리 효과
        """
        if not self.client or not candidates:
            return []

        lines = []
        for c in candidates[:200]:
            lines.append(
                f"- {c.get('name', c['ticker'])}({c['ticker']}) | 섹터:{c.get('sector','-')} | "
                f"가격:${c.get('price',0):.2f} | 20일모멘텀:{c.get('momentum_20d',0):+.1f}% | "
                f"RSI:{c.get('rsi',50):.1f} | 골든크로스:{'✓' if c.get('golden') else '✗'} | "
                f"MACD:{c.get('macd_state', '-')} | "
                f"종합점수:{c.get('score',0):.1f}"
            )
        candidate_text = "\n".join(lines)

        prompt = f"""[US 코어 종목 최종 선정 요청]

━━ US 코어 슬롯 투자 목표 ━━
• 시장: 미국 NASDAQ/NYSE
• 보유 기간: 장기 (1년~수년, 지속 누적 매수)
• 목표: 주식 수를 꾸준히 늘리며 복리로 자산을 키우는 "미국 기둥 종목"
• 우선순위: ① 미국 시장 섹터 대장주 (AI, 반도체, 우주/방산 리더)
             ② 강력한 성장 내러티브 (AI/로봇/우주 테마 리더)
             ③ 글로벌 경쟁 우위 — 독점적 기술·시장 지위
             ④ 장기 하락장에서도 버틸 펀더멘털 or 미래 성장 확실성
• 선호 타입: NVDA(AI 대장), TSLA(전기차/로봇), RKLB(우주 개척자)
• 배제: 레버리지 ETF, 단순 배당주, 성장 없는 가치주

━━ MACD 역추세 진입 우선순위 ━━
• MACD "눌림반등(최적)" = 장기 추세는 살아있으나 단기 눌림 후 반등 시작
  → RSI 저평가 + 눌림반등 조합이면 장기 코어 누적 최적 타이밍
• MACD "눌림목" = 단기 조정 중 → 분할 매수 시작 적기
• MACD "골든크로스" = 상승 이미 반영 → 같은 조건이면 눌림목/눌림반등 종목 우선

━━ 퀀트 검증 후보 (미국 성장주) ━━
{candidate_text}

위 후보 중 "10년 뒤에도 미국 시장을 리드하고,
매달 추가 매수해도 아깝지 않은" 미국 대장주 {n}개를 선정하세요.
단기 모멘텀보다 장기 성장 내러티브와 섹터 리더십을 기준으로 판단하세요.
동점 또는 유사한 후보끼리는 MACD 눌림반등/눌림목 상태인 종목을 우선 선정하세요.

반드시 아래 JSON 형식으로만 답변하세요. 다른 텍스트는 절대 포함하지 마세요.
[
  {{"ticker": "TICKER", "reason": "선정이유(장기 누적 근거 포함)"}},
  ...
]"""
        try:
            resp = self.client.messages.create(
                model=self._FAST_MODEL,
                max_tokens=1024,
                temperature=0.2,   # 코어는 보수적 → 온도 낮게
                system=self._cached_system(),
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            if "```" in text:
                text = text.split("```")[1].lstrip("json").strip()
            selected_data = json.loads(text)

            final_selection = []
            for item in selected_data:
                for cand in candidates:
                    if cand['ticker'] == item['ticker']:
                        result = dict(cand)
                        result['ai_selected']   = True
                        result['ai_reason']     = item.get('reason', '')
                        result['strategy_name'] = '장기누적(US)'
                        final_selection.append(result)
                        break
            return final_selection[:n]
        except Exception:
            return []

    def ai_discover_satellite_themes(self) -> list[dict]:
        """
        AI가 현재 폭발 가능성 있는 테마를 발굴하고,
        각 테마별 후보 티커를 제시.
        위성 스크리닝의 첫 단계 — 하드코딩 유니버스 없이 AI가 직접 탐색.

        Returns:
            [{"theme": "양자컴퓨팅", "tickers": ["IONQ", "RGTI", "QUBT"], "reason": "..."}]
        """
        if not self.client:
            return []

        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")

        prompt = f"""[US 위성 종목 테마 발굴 요청] — {today}

당신은 미국 주식 시장의 테마 및 성장주 전문 애널리스트입니다.

━━ 발굴 목표 ━━
• "제2의 엔비디아, 테슬라, 로켓랩"이 될 가능성이 높은 미국 성장주
• 1~3개월 내 강한 상승 모멘텀이 예상되는 테마
• 아직 덜 알려졌지만 기관 수급이 들어오기 시작한 종목

━━ 선정 기준 ━━
① 지금 이 순간 가장 뜨거운 미국 시장 테마 3가지 선정
② 각 테마별로 가장 순수하게 노출된 미국 상장 종목 3~4개 제시
③ 시가총액 5억 달러 이상 (페니주 제외)
④ 실제 NASDAQ/NYSE 상장 티커만 (ETF 제외, 레버리지 ETF 절대 제외)

━━ 반드시 JSON 형식으로만 답변 ━━
[
  {{
    "theme": "테마명",
    "reason": "지금 이 테마가 폭발할 이유 (1~2문장)",
    "tickers": ["TICKER1", "TICKER2", "TICKER3"]
  }},
  ...
]"""

        try:
            resp = self.client.messages.create(
                model=self._FAST_MODEL,
                max_tokens=1024,
                temperature=0.5,   # 창의적 발굴 → 온도 높게
                system=self._cached_system(),
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            if "```" in text:
                text = text.split("```")[1].lstrip("json").strip()
            themes = json.loads(text)
            if isinstance(themes, list):
                return themes[:3]   # 최대 3개 테마
        except Exception as e:
            import logging
            logging.getLogger('lassi_bot').warning(f"[AI] 위성 테마 발굴 오류: {e}")
        return []

    def ai_approve_us_trade(self, signal: str, stock_name: str, ticker: str,
                            price_usd: float, sector: str, hot_sectors: list,
                            momentum_20d: float = 0.0, rsi: float = 50.0,
                            ai_reason: str = "", news_headlines: str = "") -> tuple:
        """US 위성 매수 신호 AI 승인 — USD 기준, 미국 성장주 관점

        Returns (decision: bool, reason: str)
        """
        if not self.client:
            return True, "API 미설정으로 자동 승인"

        action  = "매수" if signal == 'BUY' else "매도"
        hot_str = ", ".join(hot_sectors) if hot_sectors else "없음"
        news_section = f"\n최신 뉴스:\n{news_headlines}" if news_headlines.strip() else ""

        prompt = f"""[US 위성 매매 신호 최종 검토 — {action} 요청]
종목: {stock_name}({ticker}) | 신호: {action} | 현재가: ${price_usd:.2f}
섹터: {sector} | 현재 강세 섹터: {hot_str}
20일 모멘텀: {momentum_20d:+.1f}% | RSI: {rsi:.1f}
AI 선정 이유: {ai_reason if ai_reason else "퀀트 스코어 기반"}{news_section}

──────────────────────────────────────────
【미국 성장주 매수 판단 기준】
• 골든크로스(50일선>200일선) 또는 RSI 40~65 구간이면 CONFIRM 우선
• 강세 섹터 소속이면 가중 CONFIRM
• RSI > 80(과매수) 또는 20일 모멘텀 < -10%이면 REJECT
• AI/반도체/우주/방산 대장주 = 장기 성장 관점 진입 허용

【현금 보유 판단 (중요)】
• 전략 목표는 코어 50% + 위성 50% = 100% 풀매수이며, 상승장에서 현금 보유는 기회비용
• 따라서 시장이 정상적이면 CONFIRM을 기본값으로 판단
• REJECT 하여 현금을 유지하는 것이 전략적으로 올바른 경우:
  - VIX > 30 수준의 고변동성 국면
  - Fed 긴축 사이클 초입 또는 경기침체 시그널 다수 발생
  - 해당 섹터 전체가 -15% 이상 하락 국면 (섹터 붕괴)
  - 동일 종목에서 연속 손절이 발생하는 패턴

{action} 신호 실행 여부를 판단하십시오.

답변 형식 (이 형식을 반드시 준수):
DECISION: CONFIRM 또는 REJECT
REASON: (핵심 근거 1~2줄)"""

        try:
            res = self.generate_content(prompt, temperature=0.1, model=self._FAST_MODEL)
            upper = res.upper()
            decision_line = next(
                (ln for ln in upper.splitlines()
                 if ln.strip().startswith("DECISION:") or "DECISION:" in ln), ""
            )
            if decision_line:
                after_colon = decision_line.split("DECISION:", 1)[-1].strip()
                first_word  = after_colon.split()[0] if after_colon.split() else ""
                decision    = first_word == "CONFIRM"
            else:
                decision = "CONFIRM" in upper and "REJECT" not in upper
            reason = res.split("REASON:")[-1].strip() if "REASON:" in res else res.strip()
            return decision, reason
        except Exception as e:
            import logging
            logging.getLogger('lassi_bot').warning(
                f"[ClaudeAPI] ai_approve_us_trade 오류 — 자동 허용: {type(e).__name__}: {e}")
            return True, "AI 일시 오류 — 알고리즘 신호 허용"

    def ai_swing_trade_check(
        self,
        ticker: str, name: str,
        price_usd: float, avg_usd: float, pnl_pct: float,
        regime: str, exit_reason: str,
        roe_reason: str = "", news: str = "", fundamental: str = "",
        hot_sectors: list = None, accumulate_count: int = 0,
    ) -> str:
        """
        US봇 ATR 손절/트레일링 발동 시 AI 전권 판단.
        AI가 종목 전망을 종합해 3가지 중 하나 반환:
          SELL_REBUY  — 매도 후 RSI≤35 or 추가-5% 시 재매수
          ACCUMULATE  — 매도 보류 + 잔여예산 추가매수 (평단 낮추기)
          EXIT        — 기존 로직대로 전량 매도

        US봇은 100% AI 자율 운영 — 이 판단이 최우선.
        """
        if not self.client:
            return 'EXIT'
        if accumulate_count >= 2:
            return 'EXIT'  # 무한 물타기 방지

        hot_str = ", ".join(hot_sectors or []) or "없음"
        regime_label = {"BULL": "상승장 🐂", "BEAR": "하락장 🐻", "NEUTRAL": "횡보장 😐"}.get(regime, regime)
        roe_section = f"\nROE 개선 추세: {roe_reason}" if roe_reason else ""
        news_section = f"\n최신 뉴스:\n{news[:300]}" if news.strip() else ""
        fund_section = f"\n재무지표: {fundamental[:200]}" if fundamental.strip() else ""
        acc_warn = f"\n⚠️ 이미 누적 {accumulate_count}회 — 2회 초과 불가" if accumulate_count > 0 else ""

        prompt = f"""[US 자율 운영봇 — 포지션 처리 판단 요청]

종목: {name}({ticker})
현재가: ${price_usd:.2f} | 평단: ${avg_usd:.2f} | 수익률: {pnl_pct:+.1f}%
시장 국면: {regime_label} | 강세 섹터: {hot_str}
발동 이유: {exit_reason}{roe_section}{fund_section}{news_section}{acc_warn}

──────────────────────────────────────────
【판단 배경】
이 봇은 사용자가 미국 종목을 모르기 때문에 AI가 100% 자율 운영하는 공간입니다.
ATR 기계적 손절이 발동됐으나, 종목의 미래 전망에 따라 더 나은 선택이 있을 수 있습니다.

【판단 기준】
SELL_REBUY  : 추세는 살아있으나 일시 눌림 → 매도 후 RSI≤35 or 추가 -5% 시 재매수
              (단기 변동성 활용, 더 낮은 가격에 재진입)
ACCUMULATE  : 장기 전망 매우 밝음 → 매도 없이 평단 낮추며 보유 강화
              (ROE 개선 중, 섹터 성장, 강한 모멘텀 회복 예상)
EXIT        : 추세 붕괴 또는 전망 불투명 → 기존 로직대로 전량 매도

답변 형식 (반드시 준수):
DECISION: SELL_REBUY 또는 ACCUMULATE 또는 EXIT
REASON: (핵심 근거 1~2줄)"""

        try:
            res = self.generate_content(prompt, temperature=0.15, model=self._FAST_MODEL)
            upper = res.upper()
            decision_line = next(
                (ln for ln in upper.splitlines() if "DECISION:" in ln), ""
            )
            decision = 'EXIT'
            if decision_line:
                after = decision_line.split("DECISION:", 1)[-1].strip()
                if "SELL_REBUY" in after:
                    decision = 'SELL_REBUY'
                elif "ACCUMULATE" in after:
                    decision = 'ACCUMULATE'
                else:
                    decision = 'EXIT'
            reason = res.split("REASON:")[-1].strip()[:120] if "REASON:" in res else res.strip()[:120]
            import logging
            logging.getLogger('lassi_bot').info(
                f"[AI 스윙판단] {name}({ticker}) → {decision} | {reason}")
            return decision
        except Exception as e:
            import logging
            logging.getLogger('lassi_bot').warning(f"[ClaudeAPI] ai_swing_trade_check 오류: {e}")
            return 'EXIT'

    def ai_approve_split_buy(self, ticker: str, name: str,
                             price: float, avg: float, split_no: int,
                             regime: str, news: str = "") -> bool:
        """
        2차/3차 분할매수 경량 AI 체크.
        사전에 1차 매수 시 승인된 종목이므로 기본 CONFIRM.
        시장 급변(섹터 붕괴, 주요 악재)이 없으면 자동 진행.
        Returns: True(진행) / False(중단)
        """
        if not self.client:
            return True
        pnl = (price / avg - 1) * 100 if avg > 0 else 0
        news_sec = f"\n최신 뉴스: {news[:150]}" if news.strip() else ""
        prompt = f"""[{split_no}차 분할매수 속행 여부 — 빠른 판단]
종목: {name}({ticker}) | 현재 {pnl:+.1f}% | 시장: {regime}{news_sec}

이미 1차 매수 승인된 종목으로, 예약된 분할매수입니다.
다음 중 하나에 해당하면 ABORT, 그 외는 PROCEED:
- 해당 종목/섹터에 심각한 악재 발생
- 시장 국면이 BEAR로 전환되며 전체 하락 가속
- 연속 손실 패턴 확인

DECISION: PROCEED 또는 ABORT
REASON: (1줄)"""
        try:
            res = self.generate_content(prompt, temperature=0.1, model=self._FAST_MODEL)
            return "ABORT" not in res.upper()
        except Exception:
            return True  # 오류 시 진행

    def record_trade_event(self, event: str) -> None:
        """매매 결정을 대화 히스토리에 기록 — 채팅 AI가 매매 맥락을 기억하도록.
        매수/매도/거절/손절 등 주요 이벤트 발생 시 호출.
        assistant 역할로 기록해 AI 자신의 결정처럼 취급."""
        from datetime import datetime, timezone, timedelta
        _kst = datetime.now(timezone(timedelta(hours=9))).strftime('%m/%d %H:%M')
        record = f"[매매기록] {_kst} | {event}"
        self._conversation_history.append({"role": "assistant", "content": record})

    def chat(self, user_message: str, portfolio_context=None, stock_analysis_context=None) -> str:
        """대화 히스토리를 유지하는 채팅 기능 — GeminiApi.chat 호환"""
        if not self.client:
            return "❌ API 키가 등록되지 않았습니다."

        context_prefix = ""
        if portfolio_context:
            cores      = portfolio_context.get('cores', [])
            satellites = portfolio_context.get('satellites', [])
            mode_str   = "US실전" if portfolio_context.get('is_mock', True) else "KR실전"
            regime     = portfolio_context.get('market_regime', 'NEUTRAL')
            total_asset = portfolio_context.get('mock_total_asset') or portfolio_context.get('us_total_asset', 0)
            pnl        = portfolio_context.get('mock_pnl', 0)
            pnl_rt     = portfolio_context.get('mock_pnl_rt', 0)

            core_lines = []
            for c in cores:
                _avg = c.get('avg_price', 0)
                _price = c.get('price', 0)
                _pnl = ((_price / _avg - 1) * 100) if _avg > 0 and _price > 0 else 0
                core_lines.append(
                    f"  * {c['name']}({c['ticker']}): {c['shares']}주 | "
                    f"평단 {int(_avg):,}원 → 현재 {int(_price):,}원 ({_pnl:+.1f}%) | "
                    f"상태: {c.get('status','?')}"
                )

            sat_lines = []
            for s in satellites:
                _avg = s.get('avg_price', 0)
                _price = s.get('price', 0)
                _pnl = ((_price / _avg - 1) * 100) if _avg > 0 and _price > 0 else 0
                _held = f"{s['shares']}주 보유" if s.get('shares', 0) > 0 else "감시중"
                sat_lines.append(
                    f"  * {s['name']}({s['ticker']}): {_held} | "
                    f"수익률 {_pnl:+.1f}% | 전략: {s.get('strategy','-')} | "
                    f"상태: {s.get('status','?')}"
                )

            context_prefix += (
                f"[📊 현재 자산 운용 현황 — {mode_str} | 국면: {regime}]\n"
                f"■ 총자산: {int(total_asset):,}원 | 누적손익: {int(pnl):+,}원 ({pnl_rt:+.2f}%)\n"
                f"■ 코어:\n{chr(10).join(core_lines) if core_lines else '  없음'}\n"
                f"■ 위성:\n{chr(10).join(sat_lines) if sat_lines else '  없음'}\n\n"
            )

        if stock_analysis_context:
            context_prefix += f"[📈 종목 실시간 데이터]\n{stock_analysis_context}\n\n"

        # ⛔ 모든 메시지에 hallucination 방지 주입 — Haiku가 시스템 프롬프트를 무시할 때 대비
        _anti_hallucination = (
            "⛔지시: 단계별 계획·가짜 로그·근거없는 예측·[CONFIRM]태그 절대 금지. "
            "친구한테 말하듯 짧고 자연스럽게. 2~4문장.\n\n"
        )
        full_message = _anti_hallucination + context_prefix + user_message

        self._conversation_history.append({"role": "user", "content": full_message})

        # 히스토리 최대 20개로 축소 — 가짜 응답이 쌓여 few-shot 오염되는 것 방지
        if len(self._conversation_history) > 20:
            # 앞쪽 매매기록은 보존, 오래된 일반 대화만 제거
            trade_logs = [m for m in self._conversation_history if m.get('content','').startswith('[매매기록]')]
            chat_msgs  = [m for m in self._conversation_history if not m.get('content','').startswith('[매매기록]')]
            # 최근 매매기록 5개 + 최근 대화 10개
            self._conversation_history = trade_logs[-5:] + chat_msgs[-10:]

        try:
            # 채팅은 Haiku + 채팅 전용 시스템 프롬프트
            _chat_sys = [{"type": "text", "text": self._CHAT_SYSTEM,
                          "cache_control": {"type": "ephemeral"}}]
            resp = self.client.messages.create(
                model=self._SMART_MODEL,
                max_tokens=1200,   # 8192→1200: 5000자 가짜 플랜 생성 공간 원천 차단
                temperature=0.1,   # 낮은 온도 → 창의적 hallucination 억제
                system=_chat_sys,
                messages=self._conversation_history,
            )
            ai_reply = resp.content[0].text
            self._conversation_history.append({"role": "assistant", "content": ai_reply})
            return ai_reply

        except anthropic.AuthenticationError:
            return "🔑 Claude API 키가 올바르지 않습니다. [계좌 설정]에서 sk-ant-... 키를 다시 확인해 주세요."
        except anthropic.RateLimitError:
            return "⏳ API 호출 한도에 도달했습니다. 잠시 후 다시 시도해 주세요."
        except anthropic.APIStatusError as e:
            print(f"[ClaudeAPI] APIStatusError {e.status_code}: {e.message}")
            if e.status_code == 503:
                return "⏳ Claude 서버가 일시적으로 과부하 상태입니다. 잠시 후 다시 질문해 주세요."
            if e.status_code == 404:
                return f"⚠️ 모델({self.model_id})을 찾을 수 없습니다. 관리자에게 문의하세요."
            return f"⚠️ Claude API 오류 ({e.status_code}): {e.message}"
        except Exception as e:
            print(f"[ClaudeAPI] chat() 예외: {type(e).__name__}: {e}")
            return f"⚠️ AI 오류 ({type(e).__name__}): {str(e)[:120]}"

    def analyze_market(self, market_data_text: str) -> str:
        """시장 데이터 분석 리포트 생성 — GeminiApi.analyze_market 호환"""
        prompt = f"""[📊 장중 금융 시장 분석 리포트 생성 — 순수 정보 리포트]

⚠️ 이 요청은 매매 심사가 아닌 정보 요약 리포트 작성입니다.
   - [절대 투자 매뉴얼]의 데이터 게이트·거절 원칙은 이 리포트에 적용하지 마십시오.
   - 뉴스 조회 실패 종목은 "뉴스 미확인" 한 줄로만 표기하고 별도 경고 섹션을 만들지 마십시오.
   - 개별 종목에 대해 "REJECT/관망 필수" 등 매매 판단 문구를 출력하지 마십시오.
   - 제공된 지수·섹터·뉴스 데이터를 바탕으로 시장 흐름을 요약하는 것이 목적입니다.

제공된 시장 데이터(지수, 이평선, RSI, 거래량) 및 주요 종목 뉴스를 바탕으로
월스트리트 기관 투자자 관점의 간결한 '데일리 시장 분석 리포트'를 마크다운 형식으로 작성하십시오.

[시장 데이터 및 뉴스]
{market_data_text}"""
        return self.generate_content(prompt, temperature=0.7)

    def ai_approve_trade(self, signal, stock_name, ticker, price, strategy,
                         indicator_val, hot_sectors, recent_trades=None, custom_rules="",
                         context: str = "", portfolio_context: str = ""):
        """매매 신호 발생 시 AI 종합 분석 후 최종 승인 — GeminiApi.ai_approve_trade 호환
        반환: (approved: bool, reason: str, confidence: int)  ← confidence 50~100
        하위 호환: 기존 코드가 (bool, str) 2개만 언패킹해도 무방
        """
        if not self.client:
            return True, "API 미설정으로 자동 승인", 100

        action = "매수" if signal == 'BUY' else "매도"

        # ── 과거 매매 이력 + AI 판단 vs 실제 결과 피드백 ──────────────────
        history_text = "이 종목에 대한 최근 매매 기록이 없습니다."
        if recent_trades:
            lines = []
            for t in recent_trades:
                ai_hit = ""
                if t['action'] == 'SELL' and t.get('profit') is not None:
                    outcome = "✅수익" if t['profit'] > 0 else "❌손실"
                    ai_hit = f" → 실제결과: {outcome} {t['profit']:+,.0f}원"
                elif t['action'] == 'BUY':
                    ai_hit = " (매수 후 미청산)"
                ai_prev = f" | AI판단: {t['ai_reason'][:40]}" if t.get('ai_reason') else ""
                lines.append(f"- {t['date']} | {t['action']} | {t['price']:,.0f}원{ai_prev}{ai_hit}")
            history_text = "\n".join(lines)

        context_section = f"\n[📊 실시간 종합 분석 데이터]\n{context}\n" if context else ""
        portfolio_section = f"\n[💼 현재 포트폴리오 현황]\n{portfolio_context}\n" if portfolio_context else ""

        ind_str = (f"{indicator_val:.2f}" if isinstance(indicator_val, (int, float))
                   else str(indicator_val))

        prompt = f"""[매매 신호 최종 검토 — {action} 요청]
종목: {stock_name}({ticker}) | 신호: {action} | 현재가: {price:,}원
적용 전략: {strategy} | 전략 지표값: {ind_str}
{context_section}{portfolio_section}
[투자자 본인이 확립한 매매 원칙]
{custom_rules if custom_rules else "특별한 커스텀 룰 없음. 시스템 기본 원칙 적용."}

[이 종목의 과거 매매 이력 — AI 판단 vs 실제 결과 오답노트]
{history_text}

──────────────────────────────────────────
【판단 지침】
• 위에 제공된 데이터(RSI, MACD, 볼린저밴드, 거래량, 전일종가, 5일선/20일선, 외인/기관 수급, KOSPI/KOSDAQ 상대강도, 뉴스/공시, 분봉 추세)를 근거로 사용할 것.
• 포트폴리오 현황(손실 포지션 수, 현금 비중, 전체 수익률)을 반드시 고려할 것.
• 과거 오답노트에서 같은 패턴의 실수가 반복되면 더 신중하게 판단할 것.
• N/A인 항목은 판단 근거로 쓰지 말 것.
• 근거 서술 시 구체적 수치를 인용할 것 (예: RSI 77.2, 5일선 +8.3% 위, 거래량 4.3배 등).

{action} 신호의 실행 여부를 판단하십시오.

답변 형식 (이 형식을 반드시 준수):
DECISION: CONFIRM 또는 REJECT
CONFIDENCE: 50~100 사이 정수 (판단 확신도 %)
REASON: (핵심 근거 2~3줄, 구체적 수치 포함)"""

        try:
            res = self.generate_content(prompt, temperature=0.1, model=self._FAST_MODEL)
            upper = res.upper()

            # DECISION 파싱
            decision_line = next((ln for ln in upper.splitlines() if "DECISION:" in ln), "")
            if decision_line:
                after_colon = decision_line.split("DECISION:", 1)[-1].strip()
                first_word  = after_colon.split()[0] if after_colon.split() else ""
                decision = first_word == "CONFIRM"
            else:
                decision = "CONFIRM" in upper and "REJECT" not in upper

            # CONFIDENCE 파싱
            confidence = 75
            conf_line = next((ln for ln in upper.splitlines() if "CONFIDENCE:" in ln), "")
            if conf_line:
                import re as _re
                m = _re.search(r'CONFIDENCE:\s*(\d+)', conf_line)
                if m:
                    confidence = max(50, min(100, int(m.group(1))))

            reason = res.split("REASON:")[-1].strip() if "REASON:" in res else res.strip()
            return decision, reason, confidence
        except Exception as e:
            import logging
            logging.getLogger('lassi_bot').warning(
                f"[ClaudeAPI] ai_approve_trade 오류 — 알고리즘 신호 허용: {type(e).__name__}: {e}")
            return True, "AI 일시 오류 — 알고리즘 신호 그대로 허용", 75

    def ai_approve_core_trade(self, stock_name: str, ticker: str, price: int,
                               rsi: float, ma120: float, ma60: float,
                               regime: str = "NEUTRAL",
                               news_headlines: str = "") -> tuple:
        """코어 장기 매수 AI 승인 — 위성과 다른 기준 적용.

        코어는 RSI 저평가 + 120MA 위 기준을 이미 통과한 상태.
        AI 역할: 거래량·모멘텀 무관, 단기 추가 하락 리스크(악재·구조적 위기)만 판단.
        기본값 CONFIRM — 명백한 이유 있을 때만 REJECT.
        """
        if not self.client:
            return True, "API 미설정으로 자동 승인"

        ma120_gap = (price / ma120 - 1) * 100 if ma120 > 0 else 0
        ma60_gap  = (price / ma60  - 1) * 100 if ma60  > 0 else 0
        news_section = f"\n[최근 뉴스]\n{news_headlines}" if news_headlines else ""

        prompt = f"""[코어 장기 매수 리스크 검토]
종목: {stock_name}({ticker}) | 현재가: {price:,}원 | 시장국면: {regime}

[진입 근거 — 이미 기준 충족됨]
• RSI {rsi:.1f} (저평가 구간 ≤45)
• 120일선 대비 {ma120_gap:+.1f}% ({ma120:,.0f}원) — 장기 우상향 확인
• 60일선 대비 {ma60_gap:+.1f}% ({ma60:,.0f}원)
{news_section}

[판단 지침 — 코어 전용]
• 코어는 장기 저평가 매수 전략 (3개월~1년 보유 목표)
• 거래량 감소, MACD, 5일선 이탈 등 단기 모멘텀 지표는 판단 근거로 쓰지 말 것
• REJECT 기준: 심각한 악재(회계 부정, 규제 리스크, 구조적 산업 위기, 상장폐지 위험)
• 단순 단기 하락, 시장 약세는 REJECT 사유 아님 — 오히려 저평가 매수 기회
• 명백한 REJECT 사유 없으면 CONFIRM

단기 추가 하락 리스크가 심각한가? 아니면 저평가 매수 적기인가?

⚠️ 답변 형식 — 아래 형식만 허용. 다른 형식(리포트, 표, 번호목록) 절대 금지:
DECISION: CONFIRM
REASON: (핵심 근거 1줄)

또는

DECISION: REJECT
REASON: (거절 이유 1줄)

반드시 첫 줄이 "DECISION: CONFIRM" 또는 "DECISION: REJECT"로 시작해야 함."""

        try:
            res = self.generate_content(prompt, temperature=0.1, model=self._FAST_MODEL)
            upper = res.upper()
            # ① DECISION: 라인 우선 파싱
            decision_line = next((ln for ln in upper.splitlines()
                                  if "DECISION:" in ln), "")
            if decision_line:
                after_colon = decision_line.split("DECISION:", 1)[-1].strip()
                first_word  = after_colon.split()[0] if after_colon.split() else ""
                decision = first_word == "CONFIRM"
            else:
                # ② 폴백: 줄 맨 앞에 CONFIRM/REJECT 단독으로 있을 때만 인식
                # "REJECT 사유 부재", "REJECT 사유 없음" 등 문장 속 REJECT는 무시
                import re as _re
                if _re.search(r'(?:^|\n)\s*CONFIRM\s*(?:\n|$)', upper):
                    decision = True
                elif _re.search(r'(?:^|\n)\s*REJECT\s*(?:\n|$)', upper):
                    decision = False
                else:
                    # 코어는 기본 CONFIRM — 명백한 거절 사유 없으면 승인
                    decision = True
            reason = res.split("REASON:")[-1].strip() if "REASON:" in res else res.strip()
            return decision, reason
        except Exception as e:
            import logging
            logging.getLogger('lassi_bot').warning(f"[ClaudeAPI] ai_approve_core_trade 오류: {e}")
            return True, "AI 일시 오류 — 자동 승인"

    def ai_partial_exit(self, ticker: str, stock_name: str, price: float,
                        avg_price: float, pnl_pct: float, shares: int,
                        partial_sold: bool, regime: str = "NEUTRAL",
                        news_headlines: str = "") -> str:
        """AI 익절 판단 — 백그라운드 스레드에서 호출됨.

        Returns: 'SELL_PARTIAL' | 'SELL_ALL' | 'HOLD'
        """
        if not self.client:
            return "SELL_PARTIAL"

        stage = "2차(나머지 전량)" if partial_sold else "1차(50%)"
        news_section = f"\n최신 뉴스 (호재 → HOLD 가중 / 악재 → SELL 가중):\n{news_headlines}" if news_headlines.strip() else ""
        prompt = f"""[익절 시점 판단 요청]
종목: {stock_name}({ticker}) | {stage} 익절 검토 중
보유주수: {shares}주 | 평균단가: {avg_price:,.0f} | 현재가: {price:,.0f} | 수익률: {pnl_pct:+.1f}%
시장 국면: {regime}{news_section}

【판단 기준】
- 호재 뉴스(수주·계약·실적 상향 등) 있으면 → 추세 지속 가능성↑ HOLD 가중
- 악재 뉴스(리콜·소송·실적 하향 등) 있으면 → 즉시 SELL 가중
- 상승 추세가 강하고 모멘텀이 살아있다면 → 추가 상승 여지 있어 HOLD
- 추세 약화 / RSI 과열(>70) / 거래량 감소 / 지지선 이탈 위험 → SELL
- 시장이 BEAR 국면이거나 급격한 방향 전환 신호 → SELL
- 목표 수익률 달성 후 기간이 길면 기회비용 감안 SELL

아래 형식으로만 답하십시오:
DECISION: SELL_PARTIAL 또는 SELL_ALL 또는 HOLD
REASON: (핵심 근거 한 줄)"""

        try:
            res = self.generate_content(prompt, temperature=0.1, model=self._FAST_MODEL)
            upper = res.upper()
            decision_line = next(
                (ln for ln in upper.splitlines() if "DECISION:" in ln), ""
            )
            after_colon = decision_line.split("DECISION:", 1)[-1].strip()
            first_word = after_colon.split()[0] if after_colon.split() else "SELL_PARTIAL"
            if first_word in ("SELL_PARTIAL", "SELL_ALL", "HOLD"):
                return first_word
            return "SELL_PARTIAL"
        except Exception:
            return "SELL_PARTIAL"

    # 시스템이 실제로 지원하는 전략 목록 (get_signal_by_strategy 매칭 기준)
    VALID_STRATEGIES = [
        "RSI(9) 30/70", "RSI(14) 30/70", "RSI(14) 40/60",
        "EMA 5/20 크로스", "EMA 3/10 크로스",
        "SMA 5/20 크로스", "SMA 3/10 크로스", "SMA 3/20 크로스",
        "MACD 크로스", "볼린저밴드 반전", "Stochastic 크로스",
        "CCI ±100", "Williams %R",
    ]

    def review_satellite_candidates(self, candidates: list, hot_sectors: list, sector_guide: str = '') -> list:
        """위성 종목·전략 AI 검토 — 부적합 종목 즉시 퇴출, 대체 전략 제안.

        Returns:
            approved: [{"ticker", "name", "strategy_name", "ai_reason", "approved": bool}]
        """
        if not self.client or not candidates:
            return [dict(c, approved=True, ai_reason="AI 비활성화 — 자동 승인") for c in candidates]

        hot_str = ", ".join(hot_sectors) if hot_sectors else "없음"
        strategy_list_str = "\n".join(f"  - {s}" for s in self.VALID_STRATEGIES)

        # 종목별 기술 지표 포함 (알고리즘이 이미 계산한 값 활용)
        cand_lines = "\n".join(
            f"{i+1}. {c['name']}({c['ticker']}) | 현재전략=[{c.get('strategy_name','?')}] | "
            f"6개월수익={c.get('return_pct',0):+.1f}% | 섹터={c.get('sector','-')} | "
            f"RSI={c.get('rsi', '?')} | 거래량비율={c.get('vol_ratio', '?')}"
            for i, c in enumerate(candidates)
        )

        sector_guide_section = f"\n[📊 섹터 가이드 / 커스텀 전략]\n{sector_guide}\n" if sector_guide else ""

        prompt = f"""당신은 한국 주식 위성 포트폴리오를 검토하는 퀀트 전문가입니다.

현재 강세 섹터 (참고용 — 가산점 기준, 필수 조건 아님): {hot_str}
{sector_guide_section}
[시스템이 지원하는 전략 목록 — 반드시 아래 중 하나만 선택]
{strategy_list_str}

[알고리즘 선정 위성 후보 (종목별 지표 포함)]
{cand_lines}

━━ 검토 기준 ━━
【핵심 목표】 현재 저평가 + 단기~중기 내 폭발 가능성이 있는 종목 선별
  · 한국 시장은 저평가 구간 → 지금 진입해서 크게 먹을 수 있는 종목을 찾는 것이 목표
  · 아직 안 터진 잠재주, 기관/외국인이 슬금슬금 들어오기 시작하는 종목 우선
  · 강세 섹터 여부는 참고 가산점일 뿐 — 비강세 섹터라도 지표가 좋으면 승인

각 종목에 대해 다음을 평가하라:
1. 저평가 또는 상승 촉매가 있는가?
   · 섹터 내 저PBR/저PER, 밸류업 수혜, 정책 테마, 실적 턴어라운드 등
2. 기술 지표상 진입 시점이 적합한가? (RSI, 거래량, 모멘텀)
3. 배정된 전략이 RSI·거래량비율·섹터 특성에 적합한가?
   · 더 적합한 전략이 위 목록에 있다면 교체하라 (반드시 목록 내 정확한 이름 사용).
4. 퇴출 기준 — approved=false (엄격히 적용, 남용 금지):
   · 과열 구간 (RSI>80, 거래량비율 5배↑ 이미 급등 후)
   · 유동성 부족 (거래량 극소)
   · 강세 섹터 불일치는 단독 퇴출 사유가 아님 — 지표가 좋으면 반드시 승인

반드시 아래 JSON 배열 형식으로만 답하라 (마크다운 코드블록 없이):
[
  {{"ticker": "종목코드", "approved": true/false, "strategy": "전략명(목록 중 하나)", "reason": "한줄이유(저평가/촉매 근거 포함)"}},
  ...
]"""

        import re as _re
        try:
            # 위성 종목 검토도 반복 판단 → Haiku
            raw = self.generate_content(prompt, temperature=0.2, model=self._FAST_MODEL)
            json_match = _re.search(r'\[[\s\S]*?\]', raw)
            if not json_match:
                raise ValueError("JSON 배열 없음")
            results = json.loads(json_match.group())
            result_map = {r['ticker']: r for r in results if 'ticker' in r}

            approved_list = []
            for c in candidates:
                ai = result_map.get(c['ticker'], {})
                # AI가 제안한 전략이 유효한 목록에 있는지 검증 — 없으면 원본 전략 유지
                ai_strategy = ai.get('strategy', '')
                if ai_strategy and ai_strategy in self.VALID_STRATEGIES:
                    final_strategy = ai_strategy
                else:
                    final_strategy = c.get('strategy_name', 'RSI(9) 30/70')

                approved_list.append({
                    **c,
                    'approved':      bool(ai.get('approved', True)),
                    'strategy_name': final_strategy,
                    'ai_reason':     ai.get('reason', '검토 완료'),
                })
            return approved_list

        except Exception as e:
            import logging
            logging.getLogger('lassi_bot').warning(
                f"review_satellite_candidates 오류 (원본 후보 유지): {e}")
            # 파싱 실패 시 → 안전 정책: 원본 candidates 그대로 반환 (자동 승인 X, 알고리즘 선정값 유지)
            return [dict(c, approved=True, ai_reason="AI 파싱 오류 — 알고리즘 원본 유지") for c in candidates]

    def generate_weekly_reflection(self, trade_history_text: str, existing_rules: str = "") -> str:
        """주간/누적 반성 — 기존 규칙을 유지하면서 학습 결과를 병합.
        기존 규칙을 통째로 교체하지 않고, 검증된 것은 강화·반증된 것은 수정·새 패턴은 추가."""
        if not self.client:
            return ""

        existing_section = f"""
[현재 적용 중인 기존 규칙 — 아래를 기반으로 수정/보완하라]
{existing_rules}
""" if existing_rules.strip() else "[기존 규칙 없음 — 새로 작성]"

        prompt = f"""당신은 AI 주식 트레이더입니다. 다음은 최근 매매 결과입니다.
{trade_history_text}

{existing_section}

위 매매 결과를 분석하여 아래 원칙에 따라 규칙을 업데이트하라:
1. 기존 규칙 중 이번 매매에서 검증된 항목 → 유지 또는 강화 (삭제 금지)
2. 기존 규칙 중 이번 매매에서 반증된 항목 → 수정 (이유 한 줄 명시)
3. 이번 매매에서 새로 발견한 패턴 → 규칙 말미에 추가 (최대 2개)
4. 전체 규칙은 마크다운 글머리 기호로 작성, 총 길이는 600자 이내로 유지

출력: 업데이트된 전체 규칙 텍스트만 출력 (설명·머리말 없이)"""

        try:
            return self.generate_content(prompt)
        except Exception:
            return ""

    def generate_emergency_reflection(self, ticker: str, stock_name: str,
                                       profit: float, ai_reason: str,
                                       existing_rules: str = "") -> str:
        """큰 손실 직후 긴급 반성 — 해당 거래 1건에서 배운 교훈만 기존 규칙에 추가/강화.
        기존 규칙은 최대한 보존하고 관련 항목 1~2개만 수정/추가."""
        if not self.client:
            return ""

        existing_section = (f"[현재 적용 규칙]\n{existing_rules}"
                            if existing_rules.strip() else "[기존 규칙 없음]")

        prompt = f"""방금 큰 손실 거래가 발생했습니다. 즉시 원인을 분석하고 규칙을 보강하라.

[손실 거래 정보]
- 종목: {stock_name} ({ticker})
- 손실: {profit:,.0f}원
- 매매 판단 근거: {ai_reason}

{existing_section}

지시:
1. 위 손실의 핵심 원인 1줄로 파악
2. 기존 규칙 중 이 손실과 관련된 항목 찾아서 강화 (없으면 새 항목 추가)
3. 나머지 기존 규칙은 그대로 유지

출력: 수정된 전체 규칙 텍스트만 출력 (설명 없이). 수정/추가된 항목 앞에 [NEW] 또는 [UPDATED] 태그 표시."""

        try:
            return self.generate_content(prompt, temperature=0.2)
        except Exception:
            return ""

    def reset_chat(self):
        """채팅 기록 초기화 — GeminiApi 호환"""
        self._conversation_history = []

    def ai_kr_market_context(self,
                              rule_score: int,
                              kospi_regime: str,
                              ewy_change: float,
                              nq_change: float,
                              usd_krw_change: float,
                              kospi_rsi: float) -> dict:
        """
        KR 장 시작 전 하이브리드 시장 판단.
        규칙 기반 점수(rule_score)를 참고하되, AI가 맥락을 종합해 최종 판단.

        Returns:
            {
              "regime":       "BULL"|"BEAR"|"NEUTRAL",
              "bias":         +1(강세) / 0(중립) / -1(약세),
              "entry_bonus":  int (-2 ~ +2),   # 진입 점수에 가산
              "reason":       str              # 판단 근거 한 줄
            }
        """
        prompt = f"""당신은 KR(한국) 주식시장 장 시작 전 분석 전문가입니다.
아래 신호들을 종합해 오늘 KR 장 방향을 판단하세요.

[규칙 기반 1차 점수]
- KOSPI200 기술적 점수: {rule_score:+d}점 → 1차 국면: {kospi_regime}
- KOSPI200 RSI(14): {kospi_rsi:.1f}

[외부 선행 신호 (원본 데이터)]
- EWY(코스피 프록시 ETF) 전일 등락: {ewy_change:+.2f}%
- NQ 선물(나스닥100) 등락: {nq_change:+.2f}%
- USD/KRW 환율 변화: {usd_krw_change:+.2f}% (양수=달러 강세=외국인 매도 압력)

[판단 기준 예시 — 참고만 할 것, 맥락 우선]
- EWY 하락 + NQ 하락 + 달러 강세 → BEAR 가중
- NQ 강세 + 달러 안정 → EWY 부진 상쇄 가능
- 신호들이 혼재할 때는 NEUTRAL 유지

반드시 아래 JSON만 출력 (설명 없이):
{{"regime":"BULL"|"BEAR"|"NEUTRAL","bias":1|0|-1,"entry_bonus":-2|-1|0|1|2,"reason":"한 줄 근거"}}"""

        try:
            raw = self.generate_content(prompt, temperature=0.2)
            import json, re
            m = re.search(r'\{[^}]+\}', raw, re.DOTALL)
            if m:
                result = json.loads(m.group())
                # 검증
                result['regime']      = result.get('regime', kospi_regime)
                result['bias']        = int(result.get('bias', 0))
                result['entry_bonus'] = max(-2, min(2, int(result.get('entry_bonus', 0))))
                result['reason']      = str(result.get('reason', ''))[:100]
                return result
        except Exception as e:
            import logging
            logging.getLogger('lassi_bot').debug(f"[AI KR 시장판단] 파싱 오류: {e}")
        return {"regime": kospi_regime, "bias": 0, "entry_bonus": 0, "reason": "AI 판단 실패 — 기술적 국면 유지"}

    def ai_us_market_context(self,
                              rule_score: int,
                              spy_regime: str,
                              nq_change: float,
                              es_change: float,
                              vix: float,
                              spy_rsi: float,
                              hot_sectors: list) -> dict:
        """
        US 장 시작 전 하이브리드 시장 판단.
        SPY 규칙 점수 + NQ/ES선물 + VIX + 섹터 흐름 → AI 최종 판단.

        Returns:
            {"regime": str, "bias": int, "entry_bonus": int, "reason": str}
        """
        sectors_str = ", ".join(hot_sectors[:5]) if hot_sectors else "없음"
        prompt = f"""당신은 미국 주식시장 장 시작 전 분석 전문가입니다.
아래 신호들을 종합해 오늘 US 장 방향을 판단하세요.

[SPY 규칙 기반 1차 점수]
- 기술적 점수: {rule_score:+d}점 → 1차 국면: {spy_regime}
- SPY RSI(14): {spy_rsi:.1f}

[선행 신호 (원본 데이터)]
- NQ선물(나스닥100) 등락: {nq_change:+.2f}%
- ES선물(S&P500) 등락: {es_change:+.2f}%
- VIX(공포지수): {vix:.1f} (20↑=불안, 30↑=공포, 15↓=안정)

[섹터 동향]
- 강세 섹터: {sectors_str}

[판단 기준 — 참고만, 맥락 우선]
- NQ/ES 동반 하락 + VIX 급등 → BEAR 강화
- NQ 강세 + VIX 안정 + 기술섹터 강세 → BULL 가능
- 신호 혼재 시 NEUTRAL 유지

반드시 아래 JSON만 출력 (설명 없이):
{{"regime":"BULL"|"BEAR"|"NEUTRAL","bias":1|0|-1,"entry_bonus":-2|-1|0|1|2,"reason":"한 줄 근거"}}"""

        try:
            raw = self.generate_content(prompt, temperature=0.2)
            import json, re
            m = re.search(r'\{[^}]+\}', raw, re.DOTALL)
            if m:
                result = json.loads(m.group())
                result['regime']      = result.get('regime', spy_regime)
                result['bias']        = int(result.get('bias', 0))
                result['entry_bonus'] = max(-2, min(2, int(result.get('entry_bonus', 0))))
                result['reason']      = str(result.get('reason', ''))[:100]
                return result
        except Exception as e:
            import logging
            logging.getLogger('lassi_bot').debug(f"[AI US 시장판단] 파싱 오류: {e}")
        return {"regime": spy_regime, "bias": 0, "entry_bonus": 0, "reason": "AI 판단 실패 — SPY 기술적 국면 유지"}

    def ai_portfolio_decision(self, portfolio_context: str, market_context: str,
                               positions_detail: str, mode: str = 'KR') -> dict:
        """
        포트폴리오 전체를 보고 AI가 오늘의 전략 + 포지션별 행동 지침 결정.
        매매 전 한 번 호출 → 결과를 봇이 실행 지침으로 활용.
        반환: {
            "overall_stance": "AGGRESSIVE|NEUTRAL|DEFENSIVE",
            "regime": "BULL|NEUTRAL|BEAR",
            "actions": [{"ticker": ..., "action": "BUY|SELL|HOLD|WATCH", "reason": ...}],
            "cash_target_pct": 0~100,
            "notes": "종합 판단 메모"
        }
        """
        if not self.client:
            return {"overall_stance": "NEUTRAL", "regime": "NEUTRAL",
                    "actions": [], "cash_target_pct": 30, "notes": "AI 미설정"}

        prompt = f"""[포트폴리오 전체 판단 요청] — {mode} 봇

[현재 시장 상황]
{market_context}

[현재 포트폴리오]
{portfolio_context}

[보유 포지션 상세]
{positions_detail}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【지시사항】
1. 현재 시장 상황과 포트폴리오를 종합 분석하라
2. 오늘의 전반적 전략 스탠스를 결정하라 (AGGRESSIVE/NEUTRAL/DEFENSIVE)
3. 각 보유 포지션에 대해 BUY(추가매수)/SELL(매도)/HOLD(유지)/WATCH(주시) 중 하나를 권고하라
4. 적정 현금 보유 비율을 제시하라 (0~100%)
5. 시장 국면을 판단하라 (BULL/NEUTRAL/BEAR)

반드시 아래 JSON 형식으로만 답변하라. 다른 텍스트 금지:
{{
  "overall_stance": "NEUTRAL",
  "regime": "NEUTRAL",
  "cash_target_pct": 30,
  "actions": [
    {{"ticker": "005930", "action": "HOLD", "reason": "이유 1줄"}},
    {{"ticker": "NEW", "action": "WATCH", "reason": "신규 관심 종목 이유"}}
  ],
  "notes": "종합 판단 1~2줄"
}}"""

        try:
            res = self.generate_content(prompt, temperature=0.2, model=self._SMART_MODEL)
            import re as _re, json as _json
            m = _re.search(r'\{[\s\S]+\}', res)
            if m:
                data = _json.loads(m.group())
                return {
                    "overall_stance": str(data.get("overall_stance", "NEUTRAL")).upper(),
                    "regime": str(data.get("regime", "NEUTRAL")).upper(),
                    "cash_target_pct": max(0, min(100, int(data.get("cash_target_pct", 30)))),
                    "actions": data.get("actions", []),
                    "notes": str(data.get("notes", ""))[:200]
                }
        except Exception as e:
            import logging
            logging.getLogger('lassi_bot').warning(f"[AI 포트폴리오 판단] 오류: {e}")
        return {"overall_stance": "NEUTRAL", "regime": "NEUTRAL",
                "actions": [], "cash_target_pct": 30, "notes": "AI 판단 실패"}

    def ai_rich_context_decision(self, signal: str, stock_name: str, ticker: str,
                                  price: float, trade_context: str,
                                  portfolio_context: str, custom_rules: str = "") -> tuple:
        """
        _build_trade_context의 풀 데이터를 받아 AI가 종합 판단.
        기존 ai_approve_trade보다 훨씬 풍부한 데이터 기반.
        반환: (approved: bool, reason: str, confidence: int)
        """
        if not self.client:
            return True, "API 미설정 — 자동 승인", 100

        action = "매수" if signal == 'BUY' else "매도"
        prompt = f"""[{action} 최종 판단 — 풀 데이터 기반]
종목: {stock_name}({ticker}) | {action} | 현재가: {price:,}

[종합 분석 데이터 (RSI/MACD/볼린저/수급/뉴스/공시/분봉 포함)]
{trade_context}

[현재 포트폴리오 현황]
{portfolio_context}

[매매 원칙]
{custom_rules or "시스템 기본 원칙 적용"}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
위 모든 데이터를 종합하여 {action} 여부를 판단하라.
N/A 항목은 무시. 구체적 수치를 근거로 사용할 것.

DECISION: CONFIRM 또는 REJECT
CONFIDENCE: 50~100
REASON: 핵심 근거 2~3줄 (수치 포함)"""

        try:
            res = self.generate_content(prompt, temperature=0.1, model=self._FAST_MODEL)
            upper = res.upper()
            decision_line = next((ln for ln in upper.splitlines() if "DECISION:" in ln), "")
            if decision_line:
                first_word = decision_line.split("DECISION:", 1)[-1].strip().split()[0] if decision_line.split("DECISION:", 1)[-1].strip().split() else ""
                decision = first_word == "CONFIRM"
            else:
                decision = "CONFIRM" in upper and "REJECT" not in upper
            conf_line = next((ln for ln in upper.splitlines() if "CONFIDENCE:" in ln), "")
            confidence = 75
            if conf_line:
                import re as _re
                m = _re.search(r'CONFIDENCE:\s*(\d+)', conf_line)
                if m:
                    confidence = max(50, min(100, int(m.group(1))))
            reason = res.split("REASON:")[-1].strip() if "REASON:" in res else res.strip()
            return decision, reason, confidence
        except Exception as e:
            import logging
            logging.getLogger('lassi_bot').warning(f"[AI 풀컨텍스트 판단] 오류: {e}")
            return True, f"AI 오류 — 자동 승인: {e}", 75
