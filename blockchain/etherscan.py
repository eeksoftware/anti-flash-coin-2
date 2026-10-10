import asyncio
from typing import Any

import aiohttp

from config import settings
from blockchain.base import BlockchainClient, BlockchainClientError


class EtherscanError(BlockchainClientError):
    """Base exception for Etherscan-related errors."""


class EtherscanRateLimitError(EtherscanError):
    """Raised when Etherscan rate-limits the request."""


class EtherscanTimeoutError(EtherscanError):
    """Raised when the Etherscan request times out."""


class EtherscanClient(BlockchainClient):
    BASE_URL = "https://api.etherscan.io/v2/api"
    # Pace all wallet pollers and history requests together. One request per
    # second stays below the observed 3/sec throttle and the 100k/day free quota.
    MIN_REQUEST_INTERVAL = 1.0

    def __init__(self) -> None:
        self.api_key = settings.etherscan_api_key

        self.session: aiohttp.ClientSession | None = None
        self._request_lock = asyncio.Lock()
        self._last_request_started: float | None = None

    async def _wait_for_request_slot(self) -> None:
        async with self._request_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self._last_request_started is not None:
                delay = self.MIN_REQUEST_INTERVAL - (now - self._last_request_started)
                if delay > 0:
                    await asyncio.sleep(delay)
            self._last_request_started = loop.time()

    async def start(self) -> None:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=10)

            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": "AntiFlashToken/1.0",
                },
            )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise EtherscanError(
                "ETHERSCAN_API_KEY yapılandırılmamış."
            )

        await self.start()

        assert self.session is not None
        await self._wait_for_request_slot()

        params = {
            **params,
            "apikey": self.api_key,
        }

        try:
            async with self.session.get(
                self.BASE_URL,
                params=params,
            ) as response:

                if response.status == 429:
                    raise EtherscanRateLimitError(
                        "Etherscan istek sınırına ulaşıldı."
                    )

                response.raise_for_status()

                data = await response.json()

        except asyncio.TimeoutError as exc:
            raise EtherscanTimeoutError(
                "Etherscan isteğinin süresi doldu."
            ) from exc

        except aiohttp.ClientError as exc:
            raise EtherscanError(
                f"Etherscan isteği başarısız: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise EtherscanError(
                "Beklenmeyen Etherscan yanıt biçimi."
            )

        return data

    async def get_token_transfers(
        self,
        wallet_address: str,
        start_block: int = 0,
        end_block: int = 99999999,
        page: int = 1,
        offset: int = 100,
    ) -> list[dict[str, Any]]:
        """Return raw ERC-20 transfer records from Ethereum's Etherscan V2 API.

        The provider fields (including ``logIndex`` and ``value``) are kept
        intact for the normalizer; ``chainid=1`` identifies this result as
        Ethereum.
        """
        params = {
            "chainid": "1",
            "module": "account",
            "action": "tokentx",
            "address": wallet_address,
            "startblock": start_block,
            "endblock": end_block,
            "page": page,
            "offset": offset,
            "sort": "desc",
        }

        data = await self._request(params)

        result = data.get("result")

        if isinstance(result, list):
            if all(isinstance(item, dict) for item in result):
                return result
            raise EtherscanError(
                "Etherscan token transfer kayıtlarının biçimi beklenenden farklı."
            )

        # Etherscan returns this as a status=0 response when the wallet has
        # no matching ERC-20 transfers. It is a successful empty poll.
        message = str(data.get("message", "")).lower()
        result_message = str(result).lower()
        if "no transactions found" in message or "no transactions found" in result_message:
            return []

        message = str(data.get("message", "Bilinmeyen Etherscan hatası"))
        detail = data.get("result")
        if isinstance(detail, str) and detail.strip():
            detail = " ".join(detail.split())[:300]
            if detail.lower() != message.lower():
                message = f"{message}: {detail}"

        raise EtherscanError(
            f"Etherscan API hatası: {message}"
        )
