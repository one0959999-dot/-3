import logging
from KR.bot import KRBotController
from US.bot import USBotController
from ai.client import get_ai_client

_log = logging.getLogger('lassi_bot')


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

            def _toss_val(key_toss, key_legacy):
                return (user_data.get(key_toss) or user_data.get(key_legacy) or '')

            if is_mock:
                toss_config = {
                    "client_id":     _toss_val('toss_client_id',     'us_app_key'),
                    "client_secret": _toss_val('toss_client_secret', 'us_app_secret'),
                    "account_seq":   _toss_val('toss_account_seq',   'us_account_no'),
                }
                self.bots[bot_key] = USBotController(
                    user_id,
                    toss_config=toss_config,
                    telegram_config=tele_config,
                    core_stocks=user_data.get('us_core_stocks'),
                    satellite_stocks=user_data.get('us_satellite_stocks'),
                )
            else:
                toss_config = {
                    "client_id":     _toss_val('toss_client_id',     'real_app_key'),
                    "client_secret": _toss_val('toss_client_secret', 'real_app_secret'),
                    "account_seq":   _toss_val('toss_account_seq',   'real_account_no'),
                }
                self.bots[bot_key] = KRBotController(
                    user_id, toss_config, tele_config,
                    core_stocks=user_data.get('core_stocks'),
                    satellite_stocks=user_data.get('satellite_stocks'),
                )

        bot = self.bots[bot_key]

        provider = (user_data.get('trade_ai_provider') or 'claude').strip()
        ai_key   = (user_data.get('trade_ai_key') or user_data.get('claude_api_key') or '').strip()
        if ai_key:
            cur_key = getattr(getattr(bot, 'claude', None), 'api_key', '')
            if bot.claude is None or cur_key != ai_key:
                try:
                    bot.claude = get_ai_client(provider, ai_key)
                except Exception as e:
                    _log.warning(f"AI 클라이언트 초기화 실패: {e}")
                    bot.claude = None

        return bot

    def get_peer_context(self, user_id: int, want_us: bool = True) -> dict | None:
        peer = self.bots.get((user_id, want_us))
        if not peer:
            return None

        if want_us:
            sat_summary = []
            for t, p in getattr(peer, 'satellite_positions', {}).items():
                if p.shares > 0 and p.avg_price_usd > 0:
                    price = getattr(peer, '_price_cache', {}).get(t, p.avg_price_usd)
                    pnl_pct = (price / p.avg_price_usd - 1) * 100
                    sat_summary.append(f"{p.name}({t}): {pnl_pct:+.1f}%")

            futures = getattr(peer, 'futures_snapshot', {})
            return {
                "market_regime":   getattr(peer, 'market_regime', 'NEUTRAL'),
                "hot_sectors":     getattr(peer, 'hot_sectors', []),
                "satellite_perf":  sat_summary,
                "is_running":      getattr(peer, 'is_running', False),
                "futures_summary": futures.get("summary", ""),
                "nq_futures":      futures.get("nq", {}),
                "es_futures":      futures.get("es", {}),
                "ewy_futures":     futures.get("ewy", {}),
                "sector_trends":   getattr(peer, 'sector_trends', []),
            }
        else:
            return {
                "market_regime": getattr(peer, 'market_regime', 'NEUTRAL'),
                "hot_sectors":   getattr(peer, 'hot_sectors', []),
                "is_running":    getattr(peer, 'is_running', False),
            }

    def stop_all(self):
        for bot in self.bots.values():
            bot.stop()

manager = BotManager()
