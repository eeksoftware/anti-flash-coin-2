"""Etherscan V2 client used by wallet monitoring and the Ethereum provider."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from blockchain.base import BlockchainClient, BlockchainClientError
from config import settings
from networks import get_network_config
from infra.cache import AsyncTTLCache
from infra.key_pool import CURRENT_PRIORITY, Outcome, PoolExhausted, RateLimitedPool
from infra.metrics import APIMetrics


ETHEREUM = get_network_config("ethereum")
logger = logging.getLogger(__name__)


class EtherscanError(BlockchainClientError):
    """Base exception for Etherscan-related errors."""


class EtherscanRateLimitError(EtherscanError):
    """Raised when Etherscan rate-limits the request."""


class EtherscanTimeoutError(EtherscanError):
    """Raised when the Etherscan request times out."""


class EtherscanClient(BlockchainClient):
    BASE_URL = "https://api.etherscan.io/v2/api"
    def __init__(self) -> None:
        self.metrics = APIMetrics()
        self.pool = RateLimitedPool(settings.etherscan_api_keys, {
            "rate": settings.etherscan_rate_per_key, "burst": settings.etherscan_burst,
            "daily_limit": settings.etherscan_daily_limit,
            "daily_soft_ratio": settings.etherscan_daily_soft_ratio, "metrics": self.metrics,
            "db_path": str((Path(settings.data_dir) if Path(settings.data_dir).is_absolute() else Path(__file__).resolve().parents[1] / settings.data_dir) / "api_usage.sqlite"), "provider": "etherscan",
        })
        self.cache = AsyncTTLCache(settings.cache_max_entries)
        self._log_dir = Path(settings.log_dir)
        if not self._log_dir.is_absolute(): self._log_dir = Path(__file__).resolve().parents[1] / self._log_dir
        self._singleflight: dict[tuple, asyncio.Task] = {}
        self._singleflight_lock = asyncio.Lock()
        self._metrics_task: asyncio.Task | None = None
        self.health_state = None
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "AntiFlashToken/1.0"},
            )
        if self._metrics_task is None or self._metrics_task.done():
            self._metrics_task = asyncio.create_task(self._write_metrics_periodically())

    async def close(self) -> None:
        if self._metrics_task:
            self._metrics_task.cancel()
            await asyncio.gather(self._metrics_task, return_exceptions=True)
            self._metrics_task = None
        if self.session and not self.session.closed:
            await self.session.close()
        self.pool.flush()

    async def _write_metrics_periodically(self) -> None:
        while True:
            await asyncio.sleep(60)
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "bot_version": os.getenv("BOT_VERSION", "unknown"),
                "provider": "etherscan",
                "window": self.metrics.take_window(),
                "cache_hits": self.metrics.cache_hits,
                "cache_misses": self.metrics.cache_misses,
                "queue_wait_avg": self.metrics.snapshot()["queue_wait_avg"],
                "queue_wait_max": self.metrics.snapshot()["queue_wait_max"],
                "monitor_interval": self.metrics.monitor_interval,
            }
            path = self._log_dir / "api_usage.log"
            await asyncio.to_thread(_append_json_line, path, record)

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.pool._keys:
            raise EtherscanError("ETHERSCAN_API_KEY yapılandırılmamış.")
        key = ("etherscan", params.get("chainid", ""), params.get("module", ""), params.get("action", ""), AsyncTTLCache.normalize(params))
        cached = self.cache.get(key)
        if cached is not None:
            self.metrics.cache_hits += 1
            return cached
        self.metrics.cache_misses += 1
        async with self._singleflight_lock:
            task = self._singleflight.get(key)
            if task is None:
                task = asyncio.create_task(self._request_uncached(params))
                self._singleflight[key] = task
        try:
            data = await asyncio.shield(task)
            ttl = _cache_ttl(params, data)
            if ttl:
                pending = params.get("action") == "eth_getTransactionByHash" and isinstance(data.get("result"), dict) and not data["result"].get("blockNumber")
                self.cache.set(key, data, ttl, pending=pending)
            return data
        finally:
            async with self._singleflight_lock:
                if self._singleflight.get(key) is task and task.done():
                    self._singleflight.pop(key, None)

    async def _request_uncached(self, params: dict[str, Any]) -> dict[str, Any]:
        await self.start()
        assert self.session is not None
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                timeout = (settings.api_acquire_timeout_background
                           if CURRENT_PRIORITY.get().value == "BACKGROUND"
                           else settings.api_acquire_timeout_interactive)
                lease = await self.pool.acquire(timeout=timeout)
            except PoolExhausted as exc:
                raise EtherscanError(str(exc)) from None
            started = time.monotonic()
            outcome = Outcome.OK
            try:
                request_params = {**params, "apikey": lease.secret.reveal()}
                async with self.session.get(self.BASE_URL, params=request_params) as response:
                    if response.status == 429:
                        raise EtherscanRateLimitError("Etherscan istek sınırına ulaşıldı.")
                    response.raise_for_status()
                    data = await response.json()
                if not isinstance(data, dict):
                    raise EtherscanError("Beklenmeyen Etherscan yanıt biçimi.")
                message = " ".join((str(data.get("message", "")), str(data.get("result", "")))).lower()
                if "no transactions found" in message:
                    return data
                if "max rate limit reached" in message or "rate limit" in message:
                    raise EtherscanRateLimitError("Etherscan istek sınırına ulaşıldı.")
                if "too many invalid api key attempts" in message:
                    outcome = Outcome.RATE_LIMITED
                    await asyncio.sleep(30)
                    continue
                if "invalid api key" in message:
                    outcome = Outcome.INVALID_KEY
                    raise EtherscanError("Etherscan API anahtarı geçersiz.")
                if "free api access is not supported for this chain" in message:
                    outcome = Outcome.UNSUPPORTED
                    raise EtherscanError("Etherscan bu zinciri desteklemiyor.")
                if "community free api limit reached" in message:
                    outcome = Outcome.QUOTA_EXHAUSTED
                    raise EtherscanError("Etherscan ortak API kotası tükendi.")
                return data
            except EtherscanRateLimitError as exc:
                outcome = Outcome.RATE_LIMITED; last_error = exc
            except asyncio.TimeoutError as exc:
                outcome = Outcome.NETWORK_ERROR; last_error = EtherscanTimeoutError("Etherscan isteğinin süresi doldu.")
                last_error.__cause__ = exc
            except aiohttp.ClientResponseError as exc:
                outcome = Outcome.RATE_LIMITED if exc.status == 429 else Outcome.NETWORK_ERROR
                last_error = EtherscanRateLimitError("Etherscan istek sınırına ulaşıldı.") if exc.status == 429 else EtherscanError("Etherscan isteği başarısız.")
            except aiohttp.ClientError as exc:
                outcome = Outcome.NETWORK_ERROR; last_error = EtherscanError("Etherscan isteği başarısız."); last_error.__cause__ = exc
            except EtherscanError:
                raise
            finally:
                self.pool.report(lease, outcome)
                self.metrics._latencies[lease.key_label].append(time.monotonic()-started)
                if outcome is Outcome.OK and self.health_state is not None:
                    self.health_state.last_api_success_at = datetime.now(timezone.utc)
            if outcome in (Outcome.RATE_LIMITED, Outcome.NETWORK_ERROR) and attempt < 2:
                await asyncio.sleep(min(4, .25*(2**attempt)) + random.uniform(0, .2))
                continue
            break
        if last_error:
            raise last_error from None
        raise EtherscanError("Etherscan isteği başarısız.")

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


def _cache_ttl(params: dict[str, Any], data: dict[str, Any]) -> float:
    action = params.get("action")
    result = data.get("result")
    if data.get("status") == "0" and "no transactions found" in str(result).lower(): return 15.0
    if action == "tokentx": return 5.0
    if action in ("getsourcecode", "getcontractcreation"): return 86400.0
    if action == "eth_getTransactionReceipt" and result is None: return 0.0
    if action in ("eth_getTransactionByHash", "eth_getTransactionReceipt"): return 0.0
    if action in ("eth_getCode", "eth_call") and result in ("0x", None): return 15.0
    return 60.0 if action in ("eth_getBlockByNumber", "eth_blockNumber") else 0.0


def _append_json_line(path: Path, record: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            modified_day = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date()
            today = datetime.now(timezone.utc).date()
            if modified_day != today:
                rotated = path.with_name(f"{path.name}.{modified_day.isoformat()}")
                if not rotated.exists(): path.replace(rotated)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        logger.warning("API kullanım metriği dosyaya yazılamadı.")
