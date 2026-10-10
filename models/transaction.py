from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal


Direction = Literal["RECEIVE", "SEND"]


@dataclass(frozen=True)
class TokenTransfer:
    network: str

    tx_hash: str
    log_index: int
    block_number: int
    timestamp: datetime

    token_address: str
    token_symbol: str
    token_name: str
    decimals: int

    amount: Decimal

    sender: str
    receiver: str
    direction: Direction
    event_key: str | None = None

    @property
    def event_id(self) -> tuple[str, str, str]:
        """
        Unique identifier for a token transfer event.

        A single transaction can contain multiple Transfer events,
        so tx_hash alone is not enough.
        """
        key = self.event_key or f"log:{self.log_index}"
        return (self.network, self.tx_hash.lower(), key)
