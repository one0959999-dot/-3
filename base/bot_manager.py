import logging
from KR.bot import KRBotController
from US.bot import USBotController
from ai.claude_api import ClaudeApi
from base.perplexity_client import PerplexityClient

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

        api_key = (user_data.get('claude_api_key') or '').strip()
        if api_key:
            if bot.claude is None or getattr(bot.claude, '_api_key', '') != api_key:
                try:
                    bot.claude = ClaudeApi(api_key=api_key)
                except Exception as e:
                    _log.warning(f"ClaudeApi 초기화 실패 (AI 비활성화): {e}")
                    bot.claude = None

        perp_key = (user_data.get('perplexity_api_key') or '').strip()
        if perp_key:
            if not getattr(bot, 'perplexity', None) or getattr(bot.perplexity, 'api_key', '') != perp_key:
                try:
                    bot.perplexity = PerplexityClient(api_key=perp_key)
                except Exception as e:
                    _log.warning(f"PerplexityClient 초기화 실패: {e}")
                    bot.perplexity = None
        elif not hasattr(bot, 'perplexity'):
            bot.perplexity = None

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
