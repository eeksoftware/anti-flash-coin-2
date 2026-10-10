"""Provider-independent blockchain data types and interface."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from models.transaction import TokenTransfer


TransactionStatus = Literal["success", "failed", "pending"]


@dataclass(frozen=True)
class TransactionInfo:
    tx_hash: str
    status: TransactionStatus
    block_number: int | None
    block_time: datetime | None
    confirmations: int | None
    from_address: str
    to_address: str | None
    native_value: Decimal = Decimal(0)
    confirmed: bool | None = None


@dataclass(frozen=True)
class ContractInfo:
    is_contract: bool
    name: str | None
    symbol: str | None
    decimals: int | None
    is_verified: bool | None = None
    creator: str | None = None
    creation_tx_hash: str | None = None
    created_at: datetime | None = None


@runtime_checkable
class ChainProvider(Protocol):
    network: str

    async def get_token_transfers(self, address: str, limit: int) -> list[TokenTransfer]: ...

    async def get_transaction(self, tx_hash: str) -> TransactionInfo | None: ...

    async def get_transaction_token_transfers(self, tx_hash: str) -> list[TokenTransfer]: ...

    async def get_contract_info(self, token_address: str) -> ContractInfo: ...
