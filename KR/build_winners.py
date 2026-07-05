# -*- coding: utf-8 -*-
"""승자(진성 급등) 데이터 — '참고서' 3단계.

stock_master에서 아티팩트 제외한 '진성' 급등 종목을 추려 섹터·패턴·거래대금 동반을 정리.
'전에 이런 경우 올랐다' 참고용. 출력: reference_data/winners.csv + lassi.db:winners
"""
import os, sqlite3
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def P(f):
    return os.path.join(ROOT, f)

con = sqlite3.connect(P('lassi.db'))
df = pd.read_csv(P('reference_data/stock_master.csv'), dtype={'ticker': str})

# 진성 승자: 생존 + clean(비아티팩트) + 큰 상승
win = df[(df.status == 'survivor') & (df.artifact_tier == 'clean') & (df.total_ret >= 2.0)].copy()
win = win.sort_values('total_ret', ascending=False)

win.to_csv(P('reference_data/winners.csv'), index=False, encoding='utf-8-sig')
win.to_sql('winners', con, if_exists='replace', index=False)
con.close()

print(f"저장: reference_data/winners.csv + lassi.db:winners ({len(win)}개, 3배+ 진성 상승)")
print(f"\n===== 진성 승자 섹터 분포(상위) =====")
print(win['sector'].fillna('(미상)').value_counts().head(12).to_string())
print(f"\n===== 패턴 분포 =====")
print(win['pattern'].value_counts().to_string())
print(f"\n===== 진성 급등 공통패턴(중앙값) =====")
print(f"  일평균 거래대금: {win['med_daily_value'].median()/1e8:.1f}억원 (아티팩트와 달리 거래 활발)")
print(f"  CAGR 중앙값: {win['cagr'].median()*100:.1f}%   MDD 중앙값: {win['mdd'].median()*100:.1f}%")
print(f"\n===== TOP 20 진성 승자 (심층 케이스 후보) =====")
print(win[['ticker', 'name', 'sector', 'total_ret', 'cagr', 'best_year', 'pattern']].head(20).to_string(index=False))
