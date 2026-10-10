import asyncio
import logging
from decimal import Decimal, InvalidOperation

import aiohttp


logger = logging.getLogger(__name__)


class DexScreenerPriceClient:
    """Fetch current USD prices by Ethereum token contract address."""

    BASE_URL = "https://api.dexscreener.com/tokens/v1/ethereum/"

    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None

    async def get_usd_prices(self, token_addresses: list[str]) -> dict[str, Decimal]:
        addresses = list(dict.fromkeys(address.lower() for address in token_addresses))
        prices: dict[str, Decimal] = {}
        if not addresses:
            return prices

        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )

        # The provider accepts up to 30 contract addresses in one request.
        for start in range(0, len(addresses), 30):
            batch = addresses[start : start + 30]
            try:
                async with self.session.get(self.BASE_URL + ",".join(batch)) as response:
                    response.raise_for_status()
                    pairs = await response.json()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                logger.warning("Tokenların USD fiyatı alınamadı: %s", exc)
                continue

            if not isinstance(pairs, list):
                logger.warning("DEX Screener beklenmeyen bir fiyat yanıtı döndürdü.")
                continue

            best_liquidity: dict[str, Decimal] = {}
            for pair in pairs:
                if not isinstance(pair, dict) or pair.get("chainId") != "ethereum":
                    continue
                base_token = pair.get("baseToken")
                if not isinstance(base_token, dict):
                    continue
                address = base_token.get("address")
                price_value = pair.get("priceUsd")
                if not isinstance(address, str) or address.lower() not in batch:
                    continue
                if price_value is None:
                    continue
                try:
                    price = Decimal(str(price_value))
                    liquidity_data = pair.get("liquidity")
                    liquidity = Decimal(str(
                        liquidity_data.get("usd", 0)
                        if isinstance(liquidity_data, dict)
                        else 0
                    ))
                except (InvalidOperation, ValueError):
                    continue
                if not price.is_finite() or price < 0:
                    continue
                if not liquidity.is_finite() or liquidity < 0:
                    liquidity = Decimal(0)
                key = address.lower()
                if key not in best_liquidity or liquidity > best_liquidity[key]:
                    prices[key] = price
                    best_liquidity[key] = liquidity

        return prices

    async def close(self) -> None:
        if self.session is not None and not self.session.closed:
            await self.session.close()
