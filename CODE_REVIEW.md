# 전체 코드베이스 검토 결과 (2026-06-24)

대상: 프로젝트 .py 약 35파일 / 3만 라인. 거래·자금 이동 핵심부터 정독 + 함수 시그니처/반환 arity 정합성 전수 검사.

## 🔴 발견 → 자동수정 완료 (실제 버그)

### 1. ai_approve_trade 반환 arity 불일치 (심각) — `0c8582c`
- `ai_approve_trade`는 **3-tuple**(decision, reason, confidence) 반환인데 KR 4곳(공시매도 1곳 + 위성AI매수 3곳)이 **2개로 언팩** → `ValueError`.
- 영향: AI 게이트 매수/매도가 per-ticker try에 걸려 "위성 매매 오류"로 로깅되며 **조용히 스킵**. AI 승인 거래가 실제로 실행 안 되던 상태.
- 수정: 4곳 모두 `decision, ai_reason, _ =` 로 정정. (`_ai_gate`/US는 `result[0],result[1]` 안전패턴이라 무사)

### 2. calculate_entry_score 옛 시그니처 호출 (심각) — `6783991`
- 현재 시그니처 `(df, price, regime, frgn_net, momentum_20d)` 인데 `_weekend_satellite_scan`·`_rescreen_satellites` 2곳이 **존재않는 kwargs**(sector_score/kis_score/dl_score/roe_bonus) + ticker를 price 자리에 전달 + 3값 언팩 → `TypeError`.
- 영향: 주말 스캔/재스크리닝의 점수 필터가 크래시 → swap_plan 미생성(종목 교체 로직 무력화).
- 수정: `calculate_entry_score(ohlcv, 종가, regime)` 2값 언팩으로 정정.

### 3. killswitch/D 관련 (앞 커밋 `4a52ecd`)
- D 손절/익절선(bt_*)이 상태저장 누락 → 재시작시 소멸 → save/restore 영속화(KR+US).
- killswitch L2가 첫 청산 실패/장마감시 재시도 불가 → halt는 매수만 막고 청산은 매 사이클 재시도로 변경.
- 방어자산 헤지매수가 killswitch 우회 → L2 halt시 차단.

## 🟢 검증 통과 (이상 없음)
- **toss_api.py**(실주문): 호가단위 반올림, KR지정가/US시장가 구분, None/예외 방어 — 정상.
- **함수 arity 전수**: get_composite_signal(4)·early_drop/overext/rsi_exit(3)·bear/bull/neutral_score(2)·rsi_signal(3)·budget_ratio·entry_threshold(1)·ai_finetune_decision(4, 6곳)·ai_approve_us_trade(2) — 호출부와 전부 일치.
- **0除算**: bb_range·s_cur 등 전부 `if >0` 가드.
- **루프 복원력**: KR 코어/위성 루프 per-ticker try/except 완비(한 종목 예외가 전체 안 죽임).
- **상태저장**: 범용 JSON 직렬화 — 신규 필드 영속 정상.
- **forecast/entry_engine/signals/killswitch**: end-to-end 런타임(KR삼성·US 국면별) 에러 0.

## 🟡 권고 (확정 후 — 위험 큰 구조변경이라 미적용)
- **US 위성 관리 루프 per-ticker try 부재**(~250라인): 한 종목 예외시 그 사이클의 나머지 종목 손절체크가 스킵됨(봇은 메인 try로 생존). KR처럼 per-ticker 보호 추가 권장 — 단 250라인 재들여쓰기/헬퍼추출 리팩터라 확정 후 진행.
- _close_sat 내부 try 없음(위 루프 보호에 포함되면 해소).

## 참고 (무해)
- 순익계산(_net_profit)이 매수수수료 0.015% 생략(표시용, 무시가능).
- US _save_state는 항상 is_mock 슬롯 사용 — 실/모의 모드는 init 토스키로 결정(런칭시 확인).
