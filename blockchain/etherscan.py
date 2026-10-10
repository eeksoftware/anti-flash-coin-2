"""Etherscan V2 client used by wallet monitoring and the Ethereum provider."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from blockchain.base import BlockchainClient, BlockchainClientError
from config import settings
from networks import get_network_config


ETHEREUM = get_network_config("ethereum")


class EtherscanError(BlockchainClientError):
    """Base exception for Etherscan-related errors."""


class EtherscanRateLimitError(EtherscanError):
    """Raised when Etherscan rate-limits the request."""


class EtherscanTimeoutError(EtherscanError):
    """Raised when the Etherscan request times out."""


class EtherscanClient(BlockchainClient):
    BASE_URL = "https://api.etherscan.io/v2/api"
    # One shared client paces wallet pollers and manual checks below the free
    # tier's observed throttle.
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
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "AntiFlashToken/1.0"},
            )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise EtherscanError("ETHERSCAN_API_KEY yapılandırılmamış.")
        await self.start()
        assert self.session is not None
        await self._wait_for_request_slot()
        request_params = {**params, "apikey": self.api_key}
        try:
            async with self.session.get(self.BASE_URL, params=request_params) as response:
                if response.status == 429:
                    raise EtherscanRateLimitError("Etherscan istek sınırına ulaşıldı.")
                response.raise_for_status()
                data = await response.json()
        except asyncio.TimeoutError as exc:
            raise EtherscanTimeoutError("Etherscan isteğinin süresi doldu.") from exc
        except aiohttp.ClientResponseError as exc:
            if exc.status == 429:
                raise EtherscanRateLimitError("Etherscan istek sınırına ulaşıldı.") from exc
            raise EtherscanError("Etherscan isteği başarısız.") from exc
        except aiohttp.ClientError as exc:
            raise EtherscanError("Etherscan isteği başarısız.") from exc
        if not isinstance(data, dict):
            raise EtherscanError("Beklenmeyen Etherscan yanıt biçimi.")
        return data

    @staticmethod
    def _raise_api_error(data: dict[str, Any], *, rpc: bool = False) -> None:
        result = data.get("result")
        message = str(data.get("message", "Bilinmeyen Etherscan hatası"))
        detail = " ".join(str(result).split())[:300] if isinstance(result, str) else ""
        if detail and detail.lower() != message.lower():
            message = f"{message}: {detail}"
        prefix = "Etherscan RPC hatası" if rpc else "Etherscan API hatası"
        if "rate limit" in message.lower() or "max calls per sec" in message.lower():
            raise EtherscanRateLimitError(f"{prefix}: {message}")
        raise EtherscanError(f"{prefix}: {message}")

    async def get_token_transfers(
        self,
        wallet_address: str,
        start_block: int = 0,
        end_block: int = 99999999,
        page: int = 1,
        offset: int = 100,
    ) -> list[dict[str, Any]]:
        """Return raw ERC-20 transfer records, retaining fields for normalizer."""
        data = await self._request({
            "chainid": str(ETHEREUM.chain_id),
            "module": "account",
            "action": "tokentx",
            "address": wallet_address,
            "startblock": start_block,
            "endblock": end_block,
            "page": page,
            "offset": offset,
            "sort": "desc",
        })
        result = data.get("result")
        if isinstance(result, list) and all(isinstance(item, dict) for item in result):
            return result
        message = str(data.get("message", "")).lower()
        result_message = str(result).lower()
        if "no transactions found" in message or "no transactions found" in result_message:
            return []
        self._raise_api_error(data)
        raise EtherscanError("Etherscan token transfer yanıtı beklenmeyen biçimde.")

    async def _proxy_request(self, action: str, **params: Any) -> Any:
        data = await self._request({
            "chainid": str(ETHEREUM.chain_id),
            "module": "proxy",
            "action": action,
            **params,
        })
        result = data.get("result")
        if data.get("status") == "0" or str(data.get("message", "")).upper() == "NOTOK":
            self._raise_api_error(data, rpc=True)
        if isinstance(result, dict) and isinstance(result.get("error"), dict):
            raise EtherscanError("Etherscan RPC isteği başarısız.")
        return result

    async def get_transaction_raw(self, tx_hash: str) -> dict[str, Any] | None:
        result = await self._proxy_request("eth_getTransactionByHash", txhash=tx_hash)
        return result if isinstance(result, dict) else None

    async def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        result = await self._proxy_request("eth_getTransactionReceipt", txhash=tx_hash)
        return result if isinstance(result, dict) else None

    async def get_block_number(self) -> int:
        return _hex_int(await self._proxy_request("eth_blockNumber"), "blockNumber")

    async def get_block(self, block_number: int) -> dict[str, Any] | None:
        result = await self._proxy_request(
            "eth_getBlockByNumber", tag=hex(block_number), boolean="false"
        )
        return result if isinstance(result, dict) else None

    async def get_code(self, address: str) -> str:
        result = await self._proxy_request("eth_getCode", address=address, tag="latest")
        if not isinstance(result, str):
            raise EtherscanError("Etherscan kod yanıtı beklenen biçimde değil.")
        return result

    async def eth_call(self, address: str, data: str) -> str:
        result = await self._proxy_request(
            "eth_call", to=address, data=data, tag="latest"
        )
        if not isinstance(result, str):
            raise EtherscanError("Etherscan kontrat yanıtı beklenen biçimde değil.")
        return result

    async def get_contract_source(self, address: str) -> dict[str, Any] | None:
        data = await self._request({
            "chainid": str(ETHEREUM.chain_id),
            "module": "contract",
            "action": "getsourcecode",
            "address": address,
        })
        self._raise_rate_limit(data)
        result = data.get("result")
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return result[0]
        return None

    async def get_contract_creation(self, address: str) -> dict[str, Any] | None:
        data = await self._request({
            "chainid": str(ETHEREUM.chain_id),
            "module": "contract",
            "action": "getcontractcreation",
            "contractaddresses": address,
        })
        self._raise_rate_limit(data)
        result = data.get("result")
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return result[0]
        return None

    @staticmethod
    def _raise_rate_limit(data: dict[str, Any]) -> None:
        detail = data.get("result")
        message = f"{data.get('message', '')} {detail if isinstance(detail, str) else ''}".lower()
        if "rate limit" in message or "max calls per sec" in message:
            raise EtherscanRateLimitError("Etherscan istek sınırına ulaşıldı.")


def _hex_int(value: Any, field_name: str) -> int:
    try:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            return int(value, 16) if value.lower().startswith("0x") else int(value)
    except ValueError:
        pass
    raise EtherscanError(f"Etherscan {field_name} yanıtı geçersiz.")
