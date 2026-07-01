"""Optional outbound alerting (e.g. for the manual sportsbook leg, or for
risk-manager halts). Posts a JSON payload to a generic webhook URL
(Slack/Discord/Telegram-compatible relay, or your own endpoint) if
ALERT_WEBHOOK_URL is configured; otherwise just logs.
"""

from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger(__name__)


class AlertDispatcher:
    def __init__(self, webhook_url: str | None = None) -> None:
        self._webhook_url = webhook_url

    async def send(self, message: str, **fields) -> None:
        logger.info("ALERT: %s | %s", message, fields)
        if not self._webhook_url:
            return
        payload = {"text": message, **fields}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self._webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=5)):
                    pass
        except aiohttp.ClientError as exc:
            logger.warning("failed to deliver alert webhook: %s", exc)

    async def order_needs_manual_action(self, order) -> None:
        await self.send(
            "Manual sportsbook leg required",
            order_id=order.order_id,
            market_ref=order.market_ref,
            price=order.price,
            size_usd=order.size_usd,
        )
