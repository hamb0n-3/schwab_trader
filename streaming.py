from __future__ import annotations

import asyncio
import logging

from schwab.streaming import StreamClient

logger = logging.getLogger("schwab_bot.streaming")


class QuoteStreamer:
    def __init__(self, client, symbols: list[str], on_quote=None):
        self.stream = StreamClient(client)
        self.symbols = symbols
        self.on_quote = on_quote or self._default_handler

    def _default_handler(self, msg):
        for item in msg.get("content", []):
            sym = item.get("key")
            last = item.get("LAST_PRICE") or item.get("3")
            bid = item.get("BID_PRICE") or item.get("1")
            ask = item.get("ASK_PRICE") or item.get("2")
            logger.info("STREAM %s last=%s bid=%s ask=%s", sym, last, bid, ask)

    async def run(self):
        await self.stream.login()
        self.stream.add_level_one_equity_handler(self.on_quote)
        await self.stream.level_one_equity_subs(self.symbols)
        logger.info("Subscribed to level-one quotes: %s", ", ".join(self.symbols))
        while True:
            await self.stream.handle_message()


def _standalone():
    from config import CONFIG
    from logging_setup import setup_logging
    from auth import get_client

    setup_logging(CONFIG.log_level, CONFIG.log_file)
    client = get_client(
        CONFIG.api_key, CONFIG.app_secret, CONFIG.callback_url, CONFIG.token_path
    )
    streamer = QuoteStreamer(client, CONFIG.symbols)
    asyncio.run(streamer.run())


if __name__ == "__main__":
    _standalone()
