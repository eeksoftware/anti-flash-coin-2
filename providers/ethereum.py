"""Ethereum ChainProvider backed by the shared Etherscan V2 client."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from blockchain.etherscan import EtherscanClient, EtherscanRateLimitError, EtherscanTimeoutError
from blockchain.normalizer import normalize_etherscan_transfers
from models.transaction import TokenTransfer
from networks import get_network_config
from analyzer.approvals import APPROVAL_TOPIC, ApprovalEvent, parse_approval_log
from providers.base import ContractInfo, TransactionInfo


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_METADATA_TTL_SECONDS = 86400.0


class EthereumProvider:
    network = "ethereum"
    supports_approvals = True

    async def get_latest_block(self) -> int:
        return await self.client.get_block_number()

    async def get_approval_events(self, owner: str, from_block: int, to_block: int) -> list[ApprovalEvent]:
        """Approval logs where the watched wallet is the owner (topic1)."""
        owner_key = self.config.normalize_address(owner)
        topic1 = "0x" + owner_key[2:].rjust(64, "0")
        raw_logs = await self.client.get_logs(from_block=from_block, to_block=to_block, topic0=APPROVAL_TOPIC, topic1=topic1)
        events = []
        for raw in raw_logs:
            event = parse_approval_log(raw, self.network)
            if event is not None and event.owner == owner_key:
                events.append(event)
        events.sort(key=lambda item: (item.block_number, item.log_index))
        return events

    def __init__(self, client: EtherscanClient | None = None) -> None:
        self.client = client or EtherscanClient()
        self.config = get_network_config(self.network)
        self._metadata_cache: dict[str, tuple[float, ContractInfo]] = {}
        self._receipt_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._block_cache: dict[int, tuple[float, dict[str, Any] | None]] = {}

    async def close(self) -> None:
        await self.client.close()

    async def get_token_transfers(self, address: str, limit: int) -> list[TokenTransfer]:
        records = await self.client.get_token_transfers(address, page=1, offset=limit)
        return normalize_etherscan_transfers(records, address)[:limit]

    async def get_token_transfer_page(
        self, address: str, page: int, limit: int
    ) -> list[TokenTransfer]:
        records = await self.client.get_token_transfers(address, page=page, offset=limit)
        return normalize_etherscan_transfers(records, address)

    async def get_transaction(self, tx_hash: str) -> TransactionInfo | None:
        tx = await self.client.get_transaction_raw(tx_hash)
        if tx is None:
            return None
        tx_id = str(tx.get("hash") or tx_hash).lower()
        from_address = str(tx.get("from") or "").lower()
        to_value = tx.get("to")
        to_address = str(to_value).lower() if isinstance(to_value, str) and to_value else None
        native_value = Decimal(_hex_int(tx.get("value", "0x0"))).scaleb(-18)
        block_value = tx.get("blockNumber")
        if block_value in (None, "0x", ""):
            return TransactionInfo(
                tx_id, "pending", None, None, None, from_address, to_address, native_value
            )

        block_number = _hex_int(block_value)
        receipt = await self._get_receipt(tx_id)
        if receipt is None:
            return TransactionInfo(
                tx_id, "pending", block_number, None, None, from_address, to_address, native_value
            )
        status_value = receipt.get("status")
        status = "success" if _hex_int(status_value) != 0 else "failed"
        latest_block = await self.client.get_block_number()
        confirmations = max(0, latest_block - block_number + 1)
        if confirmations >= 12:
            self._receipt_cache[tx_id] = (time.monotonic(), receipt)
        block = await self._get_block(block_number)
        timestamp = _hex_int(block.get("timestamp")) if block and block.get("timestamp") is not None else None
        block_time = datetime.fromtimestamp(timestamp, timezone.utc) if timestamp is not None else None
        return TransactionInfo(
            tx_hash=tx_id,
            status=status,
            block_number=block_number,
            block_time=block_time,
            confirmations=confirmations,
            from_address=from_address,
            to_address=to_address,
            native_value=native_value,
        )

    async def get_transaction_token_transfers(self, tx_hash: str) -> list[TokenTransfer]:
        receipt = await self._get_receipt(tx_hash)
        if receipt is None:
            return []
        block_value = receipt.get("blockNumber")
        block_number = _hex_int(block_value) if block_value is not None else 0
        timestamp = datetime.now(timezone.utc)
        if block_number:
            block = await self._get_block(block_number)
            if block and block.get("timestamp") is not None:
                timestamp = datetime.fromtimestamp(_hex_int(block["timestamp"]), timezone.utc)

        output: list[TokenTransfer] = []
        logs = receipt.get("logs")
        if not isinstance(logs, list):
            return output
        metadata: dict[str, ContractInfo] = {}
        for raw in logs:
            if not isinstance(raw, dict):
                continue
            topics = raw.get("topics")
            if not isinstance(topics, list) or len(topics) != 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
                continue
            token_address = str(raw.get("address") or "").lower()
            sender = _topic_address(topics[1])
            receiver = _topic_address(topics[2])
            data = raw.get("data")
            if not token_address or not sender or not receiver or not isinstance(data, str):
                continue
            try:
                raw_amount = int(data, 16)
                log_index = _hex_int(raw.get("logIndex"))
            except (TypeError, ValueError):
                continue
            info = metadata.get(token_address)
            if info is None:
                info = await self._get_transfer_metadata(token_address)
                metadata[token_address] = info
            decimals = info.decimals if info.decimals is not None else self.config.default_token_decimals
            output.append(TokenTransfer(
                network=self.network,
                tx_hash=tx_hash.lower(),
                log_index=log_index,
                block_number=block_number,
                timestamp=timestamp,
                token_address=token_address,
                token_symbol=info.symbol or "UNKNOWN",
                token_name=info.name or info.symbol or "Unknown token",
                decimals=decimals,
                amount=Decimal(f"{raw_amount}e-{decimals}"),
                sender=sender,
                receiver=receiver,
                direction="UNKNOWN",
                event_key=f"log:{log_index}",
            ))
            if len(output) >= 10:
                break
        return output

    async def get_contract_info(self, token_address: str) -> ContractInfo:
        address = token_address.lower()
        now = time.monotonic()
        cached = self._metadata_cache.get(address)
        if cached and now - cached[0] < _METADATA_TTL_SECONDS:
            return cached[1]

        code = await self.client.get_code(address)
        if not code or code == "0x":
            info = ContractInfo(False, None, None, None)
            self._metadata_cache[address] = (now, info)
            return info

        name = await _optional_call(self.client, address, "0x06fdde03", _decode_abi_text)
        symbol = await _optional_call(self.client, address, "0x95d89b41", _decode_abi_text)
        decimals = await _optional_call(self.client, address, "0x313ce567", _decode_uint)
        if decimals is not None and not 0 <= decimals <= 255:
            decimals = None
        verified = None
        creator = None
        creation_tx = None
        created_at = None
        try:
            source = await self.client.get_contract_source(address)
            if source is not None:
                verified = bool(source.get("SourceCode"))
        except EtherscanRateLimitError:
            raise
        except EtherscanTimeoutError:
            raise
        except Exception:
            pass
        try:
            creation = await self.client.get_contract_creation(address)
            if creation is not None:
                creator_value = creation.get("contractCreator")
                hash_value = creation.get("txHash")
                creator = str(creator_value).lower() if creator_value else None
                creation_tx = str(hash_value).lower() if hash_value else None
        except EtherscanRateLimitError:
            raise
        except EtherscanTimeoutError:
            raise
        except Exception:
            pass
        info = ContractInfo(True, name, symbol, decimals, verified, creator, creation_tx, created_at)
        self._metadata_cache[address] = (now, info)
        return info

    async def _get_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        cached = self._receipt_cache.get(tx_hash.lower())
        if cached and time.monotonic() - cached[0] < 86400:
            return cached[1]
        receipt = await self.client.get_transaction_receipt(tx_hash)
        if receipt is not None:
            self._receipt_cache[tx_hash.lower()] = (time.monotonic(), receipt)
        return receipt

    async def _get_block(self, block_number: int) -> dict[str, Any] | None:
        cached = self._block_cache.get(block_number)
        if cached and time.monotonic() - cached[0] < 60:
            return cached[1]
        block = await self.client.get_block(block_number)
        self._block_cache[block_number] = (time.monotonic(), block)
        return block

    async def _get_transfer_metadata(self, address: str) -> ContractInfo:
        """Fetch only symbol/decimals on receipt checks to keep calls near six."""
        cached = self._metadata_cache.get(address)
        if cached and time.monotonic() - cached[0] < _METADATA_TTL_SECONDS:
            return cached[1]
        symbol = await _optional_call(self.client, address, "0x95d89b41", _decode_abi_text)
        decimals = await _optional_call(self.client, address, "0x313ce567", _decode_uint)
        if decimals is not None and not 0 <= decimals <= 255:
            decimals = None
        return ContractInfo(True, symbol, symbol, decimals)


async def _optional_call(client: EtherscanClient, address: str, selector: str, decoder):
    try:
        return decoder(await client.eth_call(address, selector))
    except EtherscanRateLimitError:
        raise
    except EtherscanTimeoutError:
        raise
    except Exception:
        return None


def _hex_int(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.lower().startswith("0x") else int(value)
    raise ValueError("Geçersiz hexadecimal sayı")


def _topic_address(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.removeprefix("0x")
    if len(raw) != 64:
        return None
    try:
        int(raw, 16)
    except ValueError:
        return None
    return "0x" + raw[-40:].lower()


def _decode_uint(value: str) -> int:
    raw = value.removeprefix("0x")
    if len(raw) < 64:
        raise ValueError("Geçersiz uint256 yanıtı")
    return int(raw[-64:], 16)


def _decode_abi_text(value: str) -> str:
    raw = value.removeprefix("0x")
    if len(raw) < 64 or len(raw) % 2:
        raise ValueError("Geçersiz metin yanıtı")
    data = bytes.fromhex(raw)
    # Standard ABI dynamic string: offset at word 0, length at that offset.
    if len(data) >= 64:
        offset = int.from_bytes(data[:32], "big")
        if 0 <= offset <= len(data) - 32:
            length = int.from_bytes(data[offset:offset + 32], "big")
            end = offset + 32 + length
            if end <= len(data):
                try:
                    return data[offset + 32:end].decode("utf-8").strip("\x00")
                except UnicodeDecodeError:
                    return ""
    # Legacy tokens sometimes return a bytes32 value directly.
    return data[:32].split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
