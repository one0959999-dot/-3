from KR.bot import KRBotController   # KR 실전 봇
from US.bot import USBotController   # US 실전 매매 봇
from claude.claude_api import ClaudeApi
from KR.kis.real_api import KisRealApi


class BotManager:
    def __init__(self):
        self.bots = {}

    def get_bot(self, user_id, user_data=None):
        if not user_data:
            return self.bots.get((user_id, True))

        is_mock = bool(user_data.get('is_mock', 1))
        bot_key = (user_id, is_mock)

        if bot_key not in self.bots:
            tele_config = {
                "token": user_data.get('telegram_token'),
                "chat_id": user_data.get('telegram_chat_id')
            }
            if is_mock:
                # ── US 모드 (is_mock=True) → 미국장 실전 매매 봇 (KIS 해외주식) ──
                us_kis_config = {
                    "app_key":    user_data.get('us_app_key'),
                    "app_secret": user_data.get('us_app_secret'),
                    "account_no": user_data.get('us_account_no'),
                }
                self.bots[bot_key] = USBotController(
                    user_id,
                    kis_config=us_kis_config,
                    telegram_config=tele_config,
                    core_stocks=user_data.get('us_core_stocks'),
                    satellite_stocks=user_data.get('us_satellite_stocks'),
                )
            else:
                # ── KR 모드 (is_mock=False) → 한국 실전 봇 ──
                kis_config = {
                    "app_key":    user_data.get('real_app_key'),
                    "app_secret": user_data.get('real_app_secret'),
                    "account_no": user_data.get('real_account_no'),
                }
                self.bots[bot_key] = KRBotController(
                    user_id, kis_config, tele_config,
                    core_stocks=user_data.get('core_stocks'),
                    satellite_stocks=user_data.get('satellite_stocks'),
                )

        bot = self.bots[bot_key]

        # KR 실전봇: real_kis 인스턴스 주입 (외인/기관 데이터 조회용)
        # US 봇(is_mock=True)은 yfinance만 사용하므로 KIS 주입 불필요
        if not is_mock:
            real_bot = self.bots.get((user_id, False))
            if real_bot and getattr(real_bot, 'real_kis', None) is None:
                if user_data.get('real_app_key') and user_data.get('real_app_secret'):
                    try:
                        app_key = user_data['real_app_key'].strip()
                        existing = getattr(bot, 'real_kis', None)
                        if not existing or getattr(existing, 'app_key', '') != app_key:
                            bot.real_kis = KisRealApi(
                                app_key=app_key,
                                app_secret=user_data['real_app_secret'].strip(),
                                account_no=(user_data.get('real_account_no') or '').strip(),
                            )
                    except Exception:
                        pass

        # 각 봇은 자체 ClaudeApi 인스턴스를 가짐 — 유저 간 채팅 히스토리 공유 방지
        api_key = (user_data.get('claude_api_key') or '').strip()
        if api_key:
            if bot.claude is None or getattr(bot.claude, '_api_key', '') != api_key:
                try:
                    bot.claude = ClaudeApi(api_key=api_key)
                    # anthropic 미설치 시 client=None → AI 기능 비활성화, 봇은 정상 동작
                except Exception as e:
                    import logging
                    logging.getLogger('lassi_bot').warning(f"ClaudeApi 초기화 실패 (AI 비활성화): {e}")
                    bot.claude = None

        return bot

    def get_peer_context(self, user_id: int, want_us: bool = True) -> dict | None:
        """
        두 봇 간 시장 컨텍스트 공유 인터페이스.
        want_us=True  → US 봇 컨텍스트 반환 (KR 봇이 소비)
        want_us=False → KR 봇 컨텍스트 반환 (US 봇이 소비)
        """
        peer = self.bots.get((user_id, want_us))
        if not peer:
            return None

        if want_us:
            # US → KR: 미국장 국면 + 주도 섹터 + 보유 위성 성과 + 선물 + 섹터 추세
            sat_summary = []
            for t, p in getattr(peer, 'satellite_positions', {}).items():
                if p.shares > 0 and p.avg_price_usd > 0:
                    price = getattr(peer, '_price_cache', {}).get(t, p.avg_price_usd)
                    pnl_pct = (price / p.avg_price_usd - 1) * 100
                    sat_summary.append(f"{p.name}({t}): {pnl_pct:+.1f}%")

            futures = getattr(peer, 'futures_snapshot', {})
            return {
                "market_regime":    getattr(peer, 'market_regime', 'NEUTRAL'),
                "hot_sectors":      getattr(peer, 'hot_sectors', []),
                "satellite_perf":   sat_summary,
                "is_running":       getattr(peer, 'is_running', False),
                # 선행지표
                "futures_summary":  futures.get("summary", ""),
                "nq_futures":       futures.get("nq", {}),   # 나스닥100 선물
                "es_futures":       futures.get("es", {}),   # S&P500 선물
                "ewy_futures":      futures.get("ewy", {}),  # 한국 ETF 프록시
                "sector_trends":    getattr(peer, 'sector_trends', []),
            }
        else:
            # KR → US: 한국장 국면 + 주도 섹터
            return {
                "market_regime": getattr(peer, 'market_regime', 'NEUTRAL'),
                "hot_sectors":   getattr(peer, 'hot_sectors', []),
                "is_running":    getattr(peer, 'is_running', False),
            }

    def stop_all(self):
        for bot in self.bots.values():
            bot.stop()

manager = BotManager()