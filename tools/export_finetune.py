"""
Export backtest_trade_signals → Gemini fine-tuning JSONL

Format: {"contents": [{"role": "user", ...}, {"role": "model", ...}]}

Usage:
    python tools/export_finetune.py [--mode KR|US|ALL] [--out finetune.jsonl] [--min-gain 5]
"""
import sys, os, json, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from base.database import get_db_connection

OUTCOME_LABELS = {
    'STRONG_BUY':  (5.0, 20),   # gain ≥ 5%, days_to_peak ≤ 20
    'BUY':         (2.0, 40),
    'HOLD':        (-2.0, 999),
    'SELL':        (-5.0, 999),
    'STRONG_SELL': (-999, 999),
}


def classify_outcome(row: dict) -> str:
    gain = row.get('max_gain_pct') or 0
    days = row.get('days_to_peak') or 999
    dd   = row.get('max_drawdown_pct') or 0
    direction = row.get('signal_direction', 'BUY')

    if direction == 'SELL':
        if dd <= -8:
            return 'STRONG_SELL'
        elif dd <= -3:
            return 'SELL'
        elif gain >= 5:
            return 'BAD_SELL'
        else:
            return 'HOLD'
    else:  # BUY
        if gain >= 20:                    # 고수익 → 기간 무관
            return 'STRONG_BUY'
        elif gain >= 10 and days <= 60:
            return 'STRONG_BUY'
        elif gain >= 5 and days <= 90:
            return 'BUY'
        elif gain >= 3:
            return 'WEAK_BUY'
        elif dd <= -5:
            return 'TRAP'
        else:
            return 'FLAT'


def build_input_text(row: dict) -> str:
    lines = [
        f"종목: {row['stock_name']} ({row['ticker']}) | 시장: {row['mode']} | 날짜: {row['trade_date']}",
        f"섹터: {row.get('sector') or '기타'}",
        f"신호: {row.get('signal_types') or row.get('signal_direction')} (신뢰도: {row.get('signal_count') or 1}개 동시발생)",
        "",
        "=== 기술적 지표 ===",
        f"가격: {row['price']:,.0f} | RSI: {row.get('rsi') or 'N/A'}",
    ]

    if row.get('macd') is not None and row.get('macd_signal') is not None:
        diff = round((row['macd'] or 0) - (row['macd_signal'] or 0), 4)
        lines.append(f"MACD: {row['macd']:.4f} / 시그널: {row['macd_signal']:.4f} / 차이: {diff:+.4f}")

    if row.get('bb_upper') and row.get('bb_lower'):
        bb_pos = round((row['price'] - row['bb_lower']) / (row['bb_upper'] - row['bb_lower']) * 100, 1)
        lines.append(f"볼린저밴드: 상단 {row['bb_upper']:,.0f} / 중간 {row['bb_mid']:,.0f} / 하단 {row['bb_lower']:,.0f} (위치: {bb_pos:.0f}%)")

    if row.get('sma5') or row.get('sma20'):
        sma_parts = []
        if row.get('sma5'):  sma_parts.append(f"5일 {row['sma5']:,.0f}")
        if row.get('sma20'): sma_parts.append(f"20일 {row['sma20']:,.0f}")
        if row.get('sma60'): sma_parts.append(f"60일 {row['sma60']:,.0f}")
        if row.get('sma120'): sma_parts.append(f"120일 {row['sma120']:,.0f}")
        lines.append(f"이동평균: {' | '.join(sma_parts)}")

    if row.get('vol_ratio'):
        lines.append(f"거래량 비율: {row['vol_ratio']:.0f}% (평균 대비)")

    if row.get('support') and row.get('resistance'):
        lines.append(f"지지: {row['support']:,.0f} | 저항: {row['resistance']:,.0f}")

    lines.append("")
    lines.append("=== 시장 국면 ===")
    lines.append(f"국면: {row.get('market_phase_kr') or row.get('market_phase')} (확신도: {(row.get('phase_confidence') or 0)*100:.0f}%)")
    if row.get('hot_sectors'):
        in_hot = '✅ 강세섹터 포함' if (row.get('sector') and row.get('sector') in row['hot_sectors']) else ''
        lines.append(f"당시 강세 섹터: {row['hot_sectors']} {in_hot}".rstrip())

    if row.get('macro_str'):
        lines.append("")
        lines.append("=== 거시경제 ===")
        lines.append(row['macro_str'])
    else:
        macro_parts = []
        if row.get('vix'):     macro_parts.append(f"VIX {row['vix']:.1f}")
        if row.get('usd_krw'): macro_parts.append(f"달러원 {row['usd_krw']:.0f}")
        if row.get('us_10y'):  macro_parts.append(f"미10년 {row['us_10y']:.2f}%")
        if macro_parts:
            lines.append("")
            lines.append("=== 거시경제 ===")
            lines.append(' | '.join(macro_parts))

    if row.get('news_summary'):
        lines.append("")
        lines.append("=== 공시 정보 ===")
        lines.append(row['news_summary'])

    dl = row.get('_delist')
    if dl and dl.get('reason'):
        lines.append("")
        lines.append("=== 상폐 정보 (이 종목의 최종 운명) ===")
        lines.append(f"이 종목은 {dl.get('last_date','')}에 [{dl['reason']}]로 상장폐지됨 "
                     f"(최종가 고점대비 {dl.get('last_vs_peak_pct','?')}%)")

    lines.append("")
    lines.append(f"질문: 위 조건에서 {row['signal_direction']} 신호가 발생했습니다. 향후 가격 결과를 예측하세요.")
    return '\n'.join(lines)


def build_output_text(row: dict) -> str:
    outcome = classify_outcome(row)
    gain = row.get('max_gain_pct') or 0
    days_peak = row.get('days_to_peak') or 0
    dd = row.get('max_drawdown_pct') or 0
    days_dd = row.get('days_to_max_drawdown') or 0
    days_rec = row.get('days_to_recovery') or 0

    # price path 요약 (30일 데이터만)
    path_summary = ''
    if row.get('price_path_json'):
        try:
            path = json.loads(row['price_path_json'])
            path30 = path[:30]
            if path30:
                peak30 = max(path30)
                trough30 = min(path30)
                path_summary = f"\n초반 30일: 최고 +{peak30:.1f}% / 최저 {trough30:.1f}%"
        except Exception:
            pass

    lines = [
        f"결과분류: {outcome}",
        f"최대수익: +{gain:.1f}% ({days_peak}일 후)",
        f"최대손실: {dd:.1f}% ({days_dd}일 후)",
    ]

    if days_rec and days_rec < 999:
        lines.append(f"회복기간: {days_rec}일")

    if path_summary:
        lines.append(path_summary)

    if outcome == 'STRONG_BUY':
        lines.append(f"\n판단: 강한 매수 신호 — {days_peak}일 내 {gain:.1f}% 고수익 달성.")
    elif outcome == 'BUY':
        lines.append(f"\n판단: 유효한 매수 기회 — {days_peak}일 내 {gain:.1f}% 수익.")
    elif outcome == 'WEAK_BUY':
        lines.append(f"\n판단: 약한 매수 신호 — {gain:.1f}% 소폭 수익, 리스크 대비 미약.")
    elif outcome == 'STRONG_SELL':
        lines.append(f"\n판단: 강한 매도 신호 — 이후 {abs(dd):.1f}% 급락.")
    elif outcome == 'SELL':
        lines.append(f"\n판단: 유효한 매도 신호 — 이후 {abs(dd):.1f}% 하락.")
    elif outcome == 'TRAP':
        lines.append(f"\n판단: 매수 함정 — 신호 후 {abs(dd):.1f}% 손실로 이어짐. 주의 필요.")
    elif outcome == 'BAD_SELL':
        lines.append(f"\n판단: 잘못된 매도 신호 — 이후 +{gain:.1f}% 상승. 매도 부적절.")
    else:
        lines.append(f"\n판단: 횡보 구간 — 뚜렷한 방향성 없음.")

    return '\n'.join(lines)


def export(mode: str = 'ALL', out_path: str = 'finetune.jsonl',
           min_records: int = 0, only_with_outcome: bool = True):
    with get_db_connection() as conn:
        q = '''SELECT * FROM backtest_trade_signals
               WHERE max_gain_pct IS NOT NULL'''
        params = []
        if mode != 'ALL':
            q += ' AND mode = ?'
            params.append(mode)
        q += ' ORDER BY trade_date ASC'
        rows = conn.execute(q, params).fetchall()

    rows = [dict(r) for r in rows]
    print(f"총 {len(rows)}개 신호 → JSONL 변환 시작")

    # 상폐 사유 맵 (생존자 편향 교정 — 파산/피인수 구분 컨텍스트)
    try:
        from base.database import get_delisting_map
        delist_map = get_delisting_map()
    except Exception:
        delist_map = {}

    written = 0
    skipped = 0
    with open(out_path, 'w', encoding='utf-8') as f:
        for row in rows:
            try:
                row['_delist'] = delist_map.get(row['ticker'])
                user_text  = build_input_text(row)
                model_text = build_output_text(row)
                record = {
                    "contents": [
                        {"role": "user",  "parts": [{"text": user_text}]},
                        {"role": "model", "parts": [{"text": model_text}]},
                    ]
                }
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
                written += 1
            except Exception as e:
                skipped += 1
                print(f"  SKIP {row.get('ticker')} {row.get('trade_date')}: {e}")

    print(f"완료: {written}개 작성, {skipped}개 건너뜀 → {out_path}")
    return written


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', default='ALL', choices=['KR', 'US', 'ALL'])
    parser.add_argument('--out',  default='finetune.jsonl')
    args = parser.parse_args()

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), args.out)
    export(mode=args.mode, out_path=out)
