from abc import ABC, abstractmethod
from typing import Any


class BlockchainClientError(Exception):
    """Base error raised when a blockchain provider request fails."""


class BlockchainClient(ABC):

    @abstractmethod
    async def get_token_transfers(
        self,
        wallet_address: str,
        page: int = 1,
        offset: int = 100,
    ) -> list[dict[str, Any]]:
        pass
