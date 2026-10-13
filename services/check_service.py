"""Shared token and transaction checks used by Telegram and the CLI."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from time import perf_counter
from typing import Any

from analyzer.token_analyzer import TokenAnalysis, TokenAnalyzer
from audit_log import CLASSIFICATION_LABELS, log_transaction
from models.transaction import TokenTransfer
from networks import UnsupportedNetwork, get_network_config
from providers.base import ChainProvider, ContractInfo, TransactionInfo
from services.check_limits import TTLCache


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenCheckResult:
    network: str
    token_address: str
    contract_info: ContractInfo
    analysis: TokenAnalysis | None
    is_wallet: bool = False
    cached: bool = False


@dataclass(frozen=True)
class CheckedTransfer:
    transfer: TokenTransfer
    analysis: TokenAnalysis


@dataclass(frozen=True)
class TxCheckResult:
    network: str
    tx_hash: str
    transaction: TransactionInfo | None
    transfers: tuple[CheckedTransfer, ...]
    classification: str
    cached: bool = False


class CheckService:
    TOKEN_TTL_SECONDS = 600.0
    TRANSACTION_TTL_SECONDS = 60.0

    def __init__(
        self,
        providers: dict[str, ChainProvider],
        analyzer: TokenAnalyzer,
        cache: TTLCache | None = None,
    ) -> None:
        self.providers = {name.lower(): provider for name, provider in providers.items()}
        self.analyzer = analyzer
        self.cache = cache or TTLCache()

    async def check_token(
        self,
        network: str,
        token_address: str,
        *,
        expected: str | None = None,
        note: str | None = None,
    ) -> TokenCheckResult:
        config = get_network_config(network)
        normalized_address = config.normalize_address(token_address)
        if not config.is_valid_address(normalized_address):
            raise ValueError("Geçersiz adres")
        key = f"token:{config.name}:{normalized_address}"
        cached = self.cache.get(key)
        if cached is not None:
            result = TokenCheckResult(
                cached.network, cached.token_address, cached.contract_info,
                cached.analysis, cached.is_wallet, True,
            )
            self._log_token(result, expected=expected, note=note, cached=True)
            return result

        provider = self._provider(config.name)
        started = perf_counter()
        info = await provider.get_contract_info(normalized_address)
        analysis = None if not info.is_contract else self.analyzer.analyze_token(
            config.name, normalized_address, info.symbol or ""
        )
        result = TokenCheckResult(
            config.name, normalized_address, info, analysis, not info.is_contract
        )
        self.cache.set(key, result, self.TOKEN_TTL_SECONDS)
        self._log_token(
            result, expected=expected, note=note,
            processing_ms=(perf_counter() - started) * 1000,
        )
        return result

    async def check_transaction(
        self,
        network: str,
        tx_hash: str,
        *,
        expected: str | None = None,
        note: str | None = None,
    ) -> TxCheckResult:
        config = get_network_config(network)
        normalized_hash = config.normalize_tx_hash(tx_hash)
        if not config.is_valid_tx_hash(tx_hash):
            raise ValueError("Geçersiz işlem hash'i")
        key = f"tx:{config.name}:{normalized_hash}"
        cached = self.cache.get(key)
        if cached is not None:
            result = TxCheckResult(
                cached.network, cached.tx_hash, cached.transaction,
                cached.transfers, cached.classification, True,
            )
            self._log_tx(result, expected=expected, note=note, cached=True)
            return result

        provider = self._provider(config.name)
        started = perf_counter()
        transaction = await provider.get_transaction(normalized_hash)
        if transaction is None:
            result = TxCheckResult(config.name, normalized_hash, None, (), "UNKNOWN")
            self.cache.set(key, result, self.TRANSACTION_TTL_SECONDS)
            self._log_tx(result, expected=expected, note=note, processing_ms=(perf_counter() - started) * 1000)
            return result

        transfers = await provider.get_transaction_token_transfers(normalized_hash)
        analyzed = tuple(
            CheckedTransfer(transfer, self.analyzer.analyze(transfer))
            for transfer in transfers[:10]
        )
        result = TxCheckResult(
            config.name,
            normalized_hash,
            transaction,
            analyzed,
            worst_classification(item.analysis.classification for item in analyzed),
        )
        self.cache.set(key, result, self.TRANSACTION_TTL_SECONDS)
        self._log_tx(
            result, expected=expected, note=note,
            processing_ms=(perf_counter() - started) * 1000,
        )
        return result

    def _provider(self, network: str) -> ChainProvider:
        try:
            return self.providers[network]
        except KeyError as exc:
            raise UnsupportedNetwork(network) from exc

    def _log_token(
        self,
        result: TokenCheckResult,
        *,
        expected: str | None,
        note: str | None,
        processing_ms: float = 0.0,
        cached: bool = False,
    ) -> None:
        analysis = result.analysis
        classification = analysis.classification if analysis else "UNKNOWN"
        info = result.contract_info
        log_transaction({
            "ts": datetime.now(timezone.utc),
            "source": "manual", "network": result.network,
            "tx_hash": None, "event_key": f"manual:{uuid.uuid4().hex}",
            "token_address": result.token_address,
            "token_symbol": info.symbol or "", "amount": "",
            "classification": classification,
            "classification_label": CLASSIFICATION_LABELS.get(classification, "Bilinmeyen"),
            "signals": [], "expected": expected, "note": note,
            "processing_ms": round(processing_ms, 3),
            "is_wallet": result.is_wallet,
            "cached": cached,
        })

    def _log_tx(
        self,
        result: TxCheckResult,
        *,
        expected: str | None,
        note: str | None,
        processing_ms: float = 0.0,
        cached: bool = False,
    ) -> None:
        if not result.transfers:
            native = result.transaction.native_value if result.transaction is not None else Decimal(0)
            symbol, native_label = ("TRX", "TRX transferi") if result.network == "tron" and native > 0 else (
                ("ETH", "ETH transferi") if native > 0 else ("", "")
            )
            log_transaction({
                "ts": datetime.now(timezone.utc),
                "source": "manual", "network": result.network,
                "tx_hash": result.tx_hash, "event_key": f"manual:{uuid.uuid4().hex}",
                "classification": result.classification,
                "classification_label": native_label or CLASSIFICATION_LABELS.get(result.classification, "Bilinmeyen"),
                "token_symbol": symbol,
                "amount": format(native, "f") if native > 0 else "",
                "signals": [], "expected": expected, "note": note,
                "processing_ms": round(processing_ms, 3), "cached": cached,
                "no_token_transfers": True,
            })
            return
        check_id = uuid.uuid4().hex
        for index, item in enumerate(result.transfers):
            transfer, analysis = item.transfer, item.analysis
            block_time = transfer.timestamp
            if block_time.tzinfo is None:
                block_time = block_time.replace(tzinfo=timezone.utc)
            log_transaction({
                "ts": datetime.now(timezone.utc),
                "source": "manual", "network": transfer.network,
                "tx_hash": transfer.tx_hash, "log_index": transfer.log_index,
                "event_key": f"manual:{check_id}:{index}",
                "block_number": transfer.block_number, "block_time": block_time,
                "direction": "UNKNOWN", "sender": transfer.sender,
                "receiver": transfer.receiver, "token_address": transfer.token_address,
                "token_symbol": transfer.token_symbol, "amount": format(transfer.amount, "f"),
                "classification": analysis.classification,
                "classification_label": CLASSIFICATION_LABELS.get(analysis.classification, "Bilinmeyen"),
                "signals": [], "expected": expected, "note": note,
                "processing_ms": round(processing_ms, 3), "cached": cached,
            })


_SEVERITY = {"CANONICAL": 0, "UNKNOWN": 1, "VARIANT": 2, "KNOWN_FRAUD": 3}


def worst_classification(classifications) -> str:
    return max(classifications, key=lambda value: _SEVERITY.get(value, 1), default="UNKNOWN")
