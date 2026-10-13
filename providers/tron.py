"""TRON TRC-20 provider backed by the TronGrid v1 and wallet APIs."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import aiohttp

from blockchain.base import BlockchainClientError
from config import settings
from health import utc_now
from infra.cache import AsyncTTLCache
from infra.key_pool import CURRENT_PRIORITY, Outcome, PoolExhausted, RateLimitedPool
from infra.metrics import APIMetrics
from models.transaction import TokenTransfer
from networks import get_network_config
from providers.base import ContractInfo, TransactionInfo, UnsupportedFeature

logger = logging.getLogger(__name__)
TRON = get_network_config("tron")
TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MAX_DECIMALS = 36
DEFAULT_DECIMALS = 6
SUN = Decimal(1_000_000)


class TronGridError(BlockchainClientError):
    """Safe provider-level error; never contains request credentials."""


class TronGridRateLimitError(TronGridError):
    pass


class TronGridTimeoutError(TronGridError):
    pass


class TronGridNetworkError(TronGridError):
    pass


class TronProvider:
    network = "tron"
    supports_approvals = False

    async def get_latest_block(self) -> int:
        raise UnsupportedFeature("TRON için approval izleme desteklenmiyor.")

    async def get_approval_events(self, owner: str, from_block: int, to_block: int) -> list:
        raise UnsupportedFeature("TRON için approval izleme desteklenmiyor.")

    def __init__(self, *, session: aiohttp.ClientSession | None = None, base_url: str | None = None):
        self.base_url = (base_url or settings.trongrid_base_url).rstrip("/")
        self.metrics = APIMetrics()
        data_dir = Path(settings.data_dir)
        if not data_dir.is_absolute():
            data_dir = Path(__file__).resolve().parents[1] / data_dir
        self.pool = RateLimitedPool(settings.trongrid_api_keys, {
            "rate": settings.trongrid_rate_per_key,
            "burst": settings.trongrid_burst,
            "daily_limit": settings.trongrid_daily_limit,
            "daily_soft_ratio": settings.trongrid_daily_soft_ratio,
            "metrics": self.metrics,
            "db_path": str(data_dir / "api_usage.sqlite"),
            "provider": "trongrid",
            "key_label_prefix": "tron-key",
            "rate_limit_cooldown": 30,
            "logger_label": "TronGrid",
            "api_label": "TronGrid",
        })
        if not self.pool._keys:
            logger.warning("TronGrid API anahtarı yok; TRON istekleri devre dışı.")
        self.cache = AsyncTTLCache(settings.cache_max_entries)
        self._session = session
        self._owns_session = session is None
        self.health_state = None
        self._fingerprints: dict[tuple[str, int], str] = {}
        self._fingerprint_filters: dict[str, dict[str, Any]] = {}
        self._last_timestamp: dict[str, int] = {}
        self._contract_cache: dict[str, ContractInfo] = {}
        self._metrics_task: asyncio.Task | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._metrics_task is not None:
            self._metrics_task.cancel()
            await asyncio.gather(self._metrics_task, return_exceptions=True)
            self._metrics_task = None
        self.pool.flush()
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, path: str, *, params=None, body=None, cache_ttl=0):
        if not self.pool._keys:
            raise TronGridError("TRONGRID_API_KEYS yapılandırılmamış.")
        cache_key = (method, path, repr(sorted((params or {}).items())), repr(body))
        if cache_ttl:
            cached = self.cache.get(cache_key)
            if cached is not None:
                self.metrics.cache_hits += 1
                return cached
            self.metrics.cache_misses += 1
        if self._metrics_task is None or self._metrics_task.done():
            self._metrics_task = asyncio.create_task(self._write_metrics_periodically())
        last_error = None
        for attempt in range(3):
            try:
                lease = await self.pool.acquire(
                    timeout=settings.api_acquire_timeout_background if CURRENT_PRIORITY.get().value == "BACKGROUND"
                    else settings.api_acquire_timeout_interactive
                )
            except PoolExhausted as exc:
                raise TronGridError("TronGrid API anahtarı kullanılamıyor.") from None
            started = time.monotonic()
            outcome = Outcome.OK
            try:
                session = await self._ensure_session()
                headers = {"TRON-PRO-API-KEY": lease.secret.reveal()}
                url = f"{self.base_url}/{path.lstrip('/')}"
                query = {
                    key: "true" if value is True else "false" if value is False else value
                    for key, value in (params or {}).items()
                }
                async with session.request(method, url, params=query, json=body, headers=headers) as response:
                    try:
                        payload = await response.json(content_type=None)
                    except (aiohttp.ClientError, ValueError):
                        payload = {}
                    message = str(payload.get("Error", payload.get("message", ""))) if isinstance(payload, dict) else ""
                    lowered = message.lower()
                    if response.status == 429 or "frequency limit" in lowered or "rate limit" in lowered:
                        outcome = Outcome.RATE_LIMITED
                        last_error = TronGridRateLimitError("TronGrid istek sınırına ulaşıldı.")
                    elif response.status == 401 or "invalid api key" in lowered or "api key is invalid" in lowered:
                        outcome = Outcome.INVALID_KEY
                        last_error = TronGridError("TronGrid API anahtarı geçersiz.")
                    elif response.status >= 500:
                        outcome = Outcome.NETWORK_ERROR
                        last_error = TronGridNetworkError("TronGrid geçici sunucu hatası döndürdü.")
                    elif response.status >= 400:
                        outcome = Outcome.UNSUPPORTED
                        raise TronGridError(f"TronGrid isteği reddetti (HTTP {response.status}).")
                    elif isinstance(payload, dict) and ("frequency limit" in lowered or "rate limit" in lowered):
                        outcome = Outcome.RATE_LIMITED
                        last_error = TronGridRateLimitError("TronGrid istek sınırına ulaşıldı.")
                    else:
                        if self.health_state is not None:
                            self.health_state.last_api_success_at = utc_now()
                        if cache_ttl:
                            self.cache.set(cache_key, payload, cache_ttl)
                        return payload
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                outcome = Outcome.NETWORK_ERROR
                last_error = TronGridTimeoutError("TronGrid isteği başarısız veya zaman aşımına uğradı.")
            finally:
                self.pool.report(lease, outcome)
                self.metrics._latencies[lease.key_label].append(time.monotonic() - started)
            if outcome is Outcome.INVALID_KEY and attempt < 2:
                continue
            if outcome in (Outcome.RATE_LIMITED, Outcome.NETWORK_ERROR) and attempt < 2:
                await asyncio.sleep(min(4, .25 * (2 ** attempt)))
                continue
            break
        if last_error:
            raise last_error from None
        raise TronGridError("TronGrid isteği başarısız.")

    async def _write_metrics_periodically(self):
        from blockchain.etherscan import _append_json_line
        log_dir = Path(settings.log_dir)
        if not log_dir.is_absolute():
            log_dir = Path(__file__).resolve().parents[1] / log_dir
        while True:
            await asyncio.sleep(60)
            snapshot = self.metrics.snapshot()
            record = {"ts": utc_now().isoformat(), "provider": "trongrid", "network": "tron",
                      "window": self.metrics.take_window(), "cache_hits": snapshot["cache_hits"],
                      "cache_misses": snapshot["cache_misses"], "queue_wait_avg": snapshot["queue_wait_avg"],
                      "queue_wait_max": snapshot["queue_wait_max"], "monitor_interval": snapshot["monitor_interval"]}
            await asyncio.to_thread(_append_json_line, log_dir / "api_usage.log", record)

    async def get_token_transfers(self, address: str, limit: int = 100) -> list[TokenTransfer]:
        return await self._fetch_transfers(address, limit, update_watermark=False)

    async def _fetch_transfers(self, address: str, limit: int, *, update_watermark: bool) -> list[TokenTransfer]:
        if not TRON.is_valid_address(address):
            raise ValueError("Geçersiz TRON cüzdan adresi.")
        limit = min(200, max(1, int(limit)))
        params: dict[str, Any] = {"limit": limit, "only_confirmed": settings.tron_only_confirmed, "order_by": "block_timestamp,desc"}
        timestamp = self._last_timestamp.get(address) if update_watermark else None
        if timestamp is not None:
            params["min_timestamp"] = max(0, timestamp - 1000)
        page = 1
        if (address, page) in self._fingerprints:
            params["fingerprint"] = self._fingerprints[(address, page)]
        if update_watermark:
            self._fingerprint_filters[address] = {key: value for key, value in params.items() if key != "fingerprint"}
        payload = await self._request("GET", f"/v1/accounts/{address}/transactions/trc20", params=params)
        items = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(items, list):
            raise TronGridError("TronGrid transfer yanıtı beklenmeyen biçimde.")
        transfers = []
        max_timestamp = self._last_timestamp.get(address, 0)
        for row in items:
            transfer = self._transfer_from_history_row(row, address)
            if transfer is None:
                continue
            transfers.append(transfer)
            max_timestamp = max(max_timestamp, int(transfer.timestamp.timestamp() * 1000))
        if transfers:
            if update_watermark:
                self._last_timestamp[address] = max_timestamp
        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
        next_fingerprint = meta.get("fingerprint") if isinstance(meta, dict) else None
        if isinstance(next_fingerprint, str) and next_fingerprint:
            self._fingerprints[(address, 2)] = next_fingerprint
        return transfers[:limit]

    async def get_token_transfer_page(self, address: str, page: int, limit: int) -> list[TokenTransfer]:
        if page == 1:
            self._fingerprints.pop((address, 2), None)
            return await self._fetch_transfers(address, limit, update_watermark=True)
        fingerprint = self._fingerprints.get((address, page))
        if not fingerprint:
            return []
        params = {**self._fingerprint_filters.get(address, {}), "fingerprint": fingerprint}
        payload = await self._request("GET", f"/v1/accounts/{address}/transactions/trc20", params=params)
        items = payload.get("data", []) if isinstance(payload, dict) else []
        transfers = [transfer for row in items if (transfer := self._transfer_from_history_row(row, address)) is not None]
        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
        cursor = meta.get("fingerprint") if isinstance(meta, dict) else None
        if isinstance(cursor, str) and cursor:
            self._fingerprints[(address, page + 1)] = cursor
        return transfers

    @staticmethod
    def _transfer_from_history_row(row: dict[str, Any], wallet: str) -> TokenTransfer | None:
        try:
            info = row.get("token_info") or {}
            sender = TRON.normalize_address(row["from"])
            receiver = TRON.normalize_address(row["to"])
            token = TRON.normalize_address(info["address"])
            decimals = _safe_decimals(info.get("decimals"))
            amount = Decimal(str(row["value"])) / (Decimal(10) ** decimals)
            timestamp_ms = int(row["block_timestamp"])
            tx_hash = TRON.normalize_tx_hash(str(row["transaction_id"]))
            if not TRON.is_valid_tx_hash(tx_hash) or not all(TRON.is_valid_address(x) for x in (sender, receiver, token)):
                return None
            confirmed = row.get("confirmed")
            if confirmed is None and settings.tron_only_confirmed:
                confirmed = True
            return TokenTransfer("tron", tx_hash, int(row.get("event_index", 0)), int(row.get("block", 0)),
                datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc), token,
                str(info.get("symbol") or "UNKNOWN"), str(info.get("name") or info.get("symbol") or "Unknown token"),
                decimals, amount, sender, receiver,
                "RECEIVE" if receiver == wallet else "SEND" if sender == wallet else "UNKNOWN",
                f"log:{row.get('event_index', 0)}", confirmed)
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
            return None

    async def get_transaction(self, tx_hash: str) -> TransactionInfo | None:
        tx_hash = TRON.normalize_tx_hash(tx_hash)
        if not TRON.is_valid_tx_hash(tx_hash):
            raise ValueError("Geçersiz TRON işlem kimliği.")
        body = await self._request("POST", "/wallet/gettransactionbyid", body={"value": tx_hash}, cache_ttl=2)
        if not isinstance(body, dict) or not body.get("txID"):
            return None
        info = await self._request("POST", "/wallet/gettransactioninfobyid", body={"value": tx_hash}, cache_ttl=2)
        solid = await self._request("POST", "/walletsolidity/gettransactioninfobyid", body={"value": tx_hash}, cache_ttl=3)
        if not isinstance(info, dict) or not info:
            return TransactionInfo(tx_hash, "pending", None, None, None, "", None, confirmed=False)
        ret = body.get("ret") or []
        result = str((ret[0] if ret and isinstance(ret[0], dict) else {}).get("contractRet", ""))
        receipt = info.get("receipt") if isinstance(info.get("receipt"), dict) else {}
        execution = str(receipt.get("result", result)).upper()
        status = ("pending" if not solid else
                  "success" if execution in {"SUCCESS", "SUCESS"} else "failed" if execution else "pending")
        raw_data = body.get("raw_data") or {}
        contract_data = ((raw_data.get("contract") or [{}])[0].get("parameter") or {}).get("value", {})
        owner = contract_data.get("owner_address", "")
        to = contract_data.get("to_address") or contract_data.get("contract_address")
        block = _int_or_none(info.get("blockNumber"))
        stamp = _int_or_none(info.get("blockTimeStamp"))
        block_time = datetime.fromtimestamp(stamp / 1000, timezone.utc) if stamp is not None else None
        confirmed = bool(solid) if isinstance(solid, dict) else False
        confirmations = await self._confirmations(block, confirmed)
        native_value = _native_trx(raw_data)
        return TransactionInfo(tx_hash, status, block, block_time, confirmations,
            _hex_address_or_empty(owner), _hex_address_or_none(to), native_value, confirmed)

    async def _confirmations(self, block: int | None, confirmed: bool) -> int | None:
        if block is None or not confirmed:
            return 0 if block is not None else None
        try:
            now = await self._request("POST", "/walletsolidity/getnowblock", body={}, cache_ttl=3)
        except TronGridError:
            return None
        header = ((now.get("block_header") or {}).get("raw_data") or {}) if isinstance(now, dict) else {}
        head = _int_or_none(header.get("number"))
        if head is None:
            return None
        return max(0, head - block + 1)

    async def get_transaction_token_transfers(self, tx_hash: str) -> list[TokenTransfer]:
        normalized_hash = TRON.normalize_tx_hash(tx_hash)
        info = await self._request("POST", "/wallet/gettransactioninfobyid", body={"value": normalized_hash}, cache_ttl=5)
        solid = await self._request("POST", "/walletsolidity/gettransactioninfobyid", body={"value": normalized_hash}, cache_ttl=5)
        logs = info.get("log", []) if isinstance(info, dict) else []
        timestamp_ms = _int_or_none(info.get("blockTimeStamp")) if isinstance(info, dict) else None
        block = _int_or_none(info.get("blockNumber")) if isinstance(info, dict) else None
        moment = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc) if timestamp_ms is not None else utc_now()
        output = []
        for index, entry in enumerate(logs if isinstance(logs, list) else []):
            try:
                topics = entry["topics"]
                if len(topics) != 3 or str(topics[0]).removeprefix("0x").lower() != TRANSFER_TOPIC:
                    continue
                token = TRON.address_from_hex("41" + str(entry["address"]).removeprefix("0x")[-40:])
                sender = TRON.address_from_hex("41" + str(topics[1])[-40:])
                receiver = TRON.address_from_hex("41" + str(topics[2])[-40:])
                amount_raw = int(str(entry["data"]).removeprefix("0x"), 16)
                metadata = await self.get_contract_info(token)
                decimals = _safe_decimals(metadata.decimals)
                output.append(TokenTransfer("tron", normalized_hash, _int_or_none(entry.get("logIndex")) or index,
                    block or 0, moment, token, metadata.symbol or "UNKNOWN", metadata.name or metadata.symbol or "Unknown token",
                    decimals, Decimal(amount_raw) / (Decimal(10) ** decimals), sender, receiver, "UNKNOWN",
                    f"log:{_int_or_none(entry.get('logIndex')) if _int_or_none(entry.get('logIndex')) is not None else index}",
                    bool(solid)))
                if len(output) >= 10:
                    break
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
        return output

    async def get_contract_info(self, token_address: str) -> ContractInfo:
        if not TRON.is_valid_address(token_address):
            raise ValueError("Geçersiz TRON kontrat adresi.")
        address = TRON.normalize_address(token_address)
        if address in self._contract_cache:
            return self._contract_cache[address]
        hex_address = TRON.address_to_hex(address)
        contract = await self._request("POST", "/wallet/getcontract", body={"value": hex_address, "visible": False}, cache_ttl=3600)
        if not isinstance(contract, dict) or not contract:
            return ContractInfo(False, None, None, None)
        name = await self._read_contract_string(address, "name()")
        symbol = await self._read_contract_string(address, "symbol()")
        decimals = await self._read_contract_uint(address, "decimals()")
        info = ContractInfo(True, name, symbol, decimals if decimals is not None and decimals <= MAX_DECIMALS else None)
        self._contract_cache[address] = info
        return info

    async def _constant_call(self, address: str, selector: str):
        hex_address = TRON.address_to_hex(address)
        result = await self._request("POST", "/wallet/triggerconstantcontract", body={
            "owner_address": hex_address, "contract_address": hex_address,
            "function_selector": selector, "visible": False,
        }, cache_ttl=3600)
        if not isinstance(result, dict) or not result.get("result", {}).get("result"):
            return None
        values = result.get("constant_result") or []
        return values[0] if values and isinstance(values[0], str) else None

    async def _read_contract_string(self, address: str, selector: str) -> str | None:
        raw = await self._constant_call(address, selector)
        try:
            return _decode_abi_text(raw) if raw else None
        except (ValueError, TypeError):
            return None

    async def _read_contract_uint(self, address: str, selector: str) -> int | None:
        raw = await self._constant_call(address, selector)
        try:
            return int(raw, 16) if raw else None
        except (TypeError, ValueError):
            return None


def _safe_decimals(value) -> int:
    try:
        parsed = int(value)
        return parsed if 0 <= parsed <= MAX_DECIMALS else DEFAULT_DECIMALS
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_DECIMALS


def _int_or_none(value):
    try:
        return int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _hex_address_or_none(value):
    try:
        return TRON.address_from_hex(value) if value else None
    except (ValueError, TypeError):
        return None


def _hex_address_or_empty(value):
    return _hex_address_or_none(value) or ""


def _native_trx(raw_data: Any) -> Decimal:
    """TRX moves in TransferContract, in sun. A TRC-20 call is not this field."""
    contracts = raw_data.get("contract") if isinstance(raw_data, dict) else None
    contract = contracts[0] if isinstance(contracts, list) and contracts and isinstance(contracts[0], dict) else {}
    if contract.get("type") != "TransferContract":
        return Decimal(0)
    value = ((contract.get("parameter") or {}).get("value") or {}) if isinstance(contract.get("parameter"), dict) else {}
    try:
        sun = Decimal(str(value.get("amount")))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)
    if sun < 0:
        return Decimal(0)
    return sun / SUN


def _decode_abi_text(value: str) -> str | None:
    raw = bytes.fromhex(value.removeprefix("0x"))
    if len(raw) >= 64:
        offset = int.from_bytes(raw[:32], "big")
        if offset + 32 <= len(raw):
            length = int.from_bytes(raw[offset:offset + 32], "big")
            if offset + 32 + length <= len(raw):
                return raw[offset + 32:offset + 32 + length].decode("utf-8", "replace")
    return raw[:32].split(b"\0", 1)[0].decode("utf-8", "replace") or None
