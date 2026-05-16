import json
import time
from google import genai
from google.genai import types

API_KEY = "AIzaSyCSRhPKoMsCDbbCpXsCFAnf_e7TPiyXycc"
client = genai.Client(api_key=API_KEY)

print("📂 1단계: 로컬에서 데이터셋을 직접 읽어옵니다...")
training_data = []

# jsonl 파일을 파이썬이 직접 읽어서 리스트로 만듭니다.
with open("global_market_training.jsonl", "r", encoding="utf-8") as f:
    for line in f:
        data = json.loads(line)
        # 구글이 요구하는 'TuningExample' 규격에 맞게 하나씩 포장
        training_data.append(
            types.TuningExample(
                text_input=data["text_input"],
                output=data["output"]
            )
        )

print(f"✅ 총 {len(training_data)}개의 정답지 리스트를 완벽하게 준비했습니다!")

print("🚀 2단계: AI 파인튜닝(학습)을 시작합니다. (수십 분 소요될 수 있습니다)")
job = client.tunings.tune(
    base_model="models/gemini-1.5-flash-001-tuning",
    
    # ✅ 핵심 해결: 파일 링크 대신, 방금 만든 '리스트(training_data)'를 직접 주입합니다!
    training_dataset=types.TuningDataset(
        examples=training_data
    ),
    
    config=types.CreateTuningJobConfig(
        tuned_model_display_name="Bear Market Master Bot"
    )
)

# 3단계: 작업이 끝날 때까지 30초마다 상태를 확인하며 대기 (폴링)
print("⏳ 구글 서버에서 학습을 진행 중입니다. 잠시만 기다려주세요...")
while True:
    job = client.tunings.get(name=job.name)
    print(f"현재 학습 상태: {job.state}...")
    
    if job.state in ['SUCCEEDED', 'FAILED']:
        break
    
    time.sleep(30)

# 결과 출력
if job.state == 'SUCCEEDED':
    print("\n🎉 드디어 파인튜닝이 완료되었습니다! 고생하셨습니다!")
    print(f"👉 내 전용 모델 ID: {job.tuned_model.model}")
    print("이제 이 모델 ID를 복사해서 봇 코드에 적용하세요!")
else:
    print(f"\n❌ 학습 실패: 상태를 확인하세요 ({job.state})")