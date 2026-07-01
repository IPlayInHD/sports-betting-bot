"""Live Polymarket order execution via the official `py-clob-client` SDK.

This module is intentionally the ONLY place in the codebase that can move
real money on Polymarket. It:
  * requires the optional `py-clob-client` dependency (`pip install .[live]`)
  * requires a funded Polygon wallet private key + CLOB API credentials
  * refuses to construct unless the caller explicitly passes `confirmed=True`,
    which `main.py` only does after `is_live_trading_authorized()` passes
    (config mode == "live" AND I_UNDERSTAND_THE_RISKS=true env var).

We deliberately do NOT implement raw EIP-712 order signing ourselves --
that's exactly the kind of hand-rolled crypto code that causes fund loss when
subtly wrong. The official SDK is the correct, audited way to sign and submit
CLOB orders.
"""

from __future__ import annotations

import logging
import time

from arbbot.models import Order, OrderStatus

logger = logging.getLogger(__name__)


class LiveTradingNotConfirmedError(RuntimeError):
    pass


class PolymarketLiveExecutionClient:
    def __init__(
        self,
        private_key: str,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        funder_address: str,
        confirmed: bool,
        chain_id: int = 137,  # Polygon mainnet
    ) -> None:
        if not confirmed:
            raise LiveTradingNotConfirmedError(
                "Refusing to initialize live execution client without explicit "
                "confirmation. Set mode: live in config.yaml AND "
                "I_UNDERSTAND_THE_RISKS=true in your environment."
            )
        if not all([private_key, api_key, api_secret, api_passphrase, funder_address]):
            raise ValueError("Missing one or more required Polymarket credentials")

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds, OrderArgs
            from py_clob_client.order_builder.constants import BUY
        except ImportError as e:
            raise ImportError(
                "py-clob-client is required for live trading. Install with: "
                "pip install '.[live]'"
            ) from e

        self._OrderArgs = OrderArgs
        self._BUY = BUY
        self._client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=chain_id,
            creds=ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase),
            funder=funder_address,
        )

    async def place_order(self, order: Order) -> Order:
        """Places a limit BUY order for the given token/side at `order.price`.

        Polymarket outcome tokens are always bought (never "sold short") --
        taking the opposite side of a market means buying the *other*
        outcome's token. `order.market_ref` must be the CLOB token_id for the
        side already selected upstream (Side.YES or Side.NO resolved to a
        concrete token_id by the caller).
        """
        order_args = self._OrderArgs(
            token_id=order.market_ref,
            price=order.price,
            size=order.size_usd / order.price if order.price > 0 else 0.0,
            side=self._BUY,
        )
        signed = self._client.create_order(order_args)
        resp = self._client.post_order(signed)

        order.submitted_at = order.submitted_at or time.time()
        if resp.get("success"):
            order.status = OrderStatus.SUBMITTED
        else:
            order.status = OrderStatus.REJECTED
            logger.error("Polymarket order rejected: %s", resp)
        return order

    async def cancel_order(self, exchange_order_id: str) -> bool:
        resp = self._client.cancel(exchange_order_id)
        return bool(resp.get("success"))
