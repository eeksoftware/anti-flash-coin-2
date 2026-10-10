"""Supported network settings and address validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Pattern


class UnsupportedNetwork(ValueError):
    """Raised when a network is not registered by this application."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Desteklenmeyen ağ: {name or '(boş)'}")
        self.name = name


@dataclass(frozen=True)
class NetworkConfig:
    name: str
    display_name: str
    chain_id: int
    address_regex: Pattern[str]
    tx_hash_regex: Pattern[str]
    official_usdt_contract: str
    tx_explorer_url_template: str
    address_explorer_url_template: str
    token_explorer_url_template: str
    default_token_decimals: int

    def transaction_url(self, tx_hash: str) -> str:
        return self.tx_explorer_url_template.format(value=tx_hash)

    def address_url(self, address: str) -> str:
        return self.address_explorer_url_template.format(value=address)

    def token_url(self, address: str) -> str:
        return self.token_explorer_url_template.format(value=address)


NETWORKS: dict[str, NetworkConfig] = {
    "ethereum": NetworkConfig(
        name="ethereum",
        display_name="Ethereum",
        chain_id=1,
        address_regex=re.compile(r"^0x[a-fA-F0-9]{40}$"),
        tx_hash_regex=re.compile(r"^0x[a-fA-F0-9]{64}$"),
        official_usdt_contract="0xdAC17F958D2ee523a2206206994597C13D831ec7",
        tx_explorer_url_template="https://etherscan.io/tx/{value}",
        address_explorer_url_template="https://etherscan.io/address/{value}",
        token_explorer_url_template="https://etherscan.io/token/{value}",
        default_token_decimals=18,
    ),
}


def get_network_config(name: str) -> NetworkConfig:
    normalized = name.strip().lower()
    try:
        return NETWORKS[normalized]
    except KeyError as exc:
        raise UnsupportedNetwork(normalized) from exc
