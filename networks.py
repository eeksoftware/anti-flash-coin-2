"""Supported network settings and address validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Pattern

import base58


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
    family: str = "evm"

    def normalize_address(self, address: str) -> str:
        return address.lower() if self.family == "evm" else address

    def is_valid_address(self, address: str) -> bool:
        if not isinstance(address, str):
            return False
        if self.family == "evm":
            return self.address_regex.fullmatch(address) is not None
        if len(address) != 34 or not address.startswith("T"):
            return False
        try:
            payload = base58.b58decode_check(address)
        except (ValueError, TypeError):
            return False
        return len(payload) == 21 and payload[0] == 0x41

    def is_valid_tx_hash(self, tx_hash: str) -> bool:
        if not isinstance(tx_hash, str):
            return False
        candidate = tx_hash[2:] if self.family == "tron" and tx_hash.lower().startswith("0x") else tx_hash
        return self.tx_hash_regex.fullmatch(candidate) is not None

    def normalize_tx_hash(self, tx_hash: str) -> str:
        value = tx_hash[2:] if self.family == "tron" and tx_hash.lower().startswith("0x") else tx_hash
        return value.lower()

    def address_to_hex(self, address: str, *, prefix: bool = False) -> str:
        if self.family != "tron":
            return self.normalize_address(address)
        if self.is_valid_address(address):
            raw = base58.b58decode_check(address).hex()
        else:
            raw = address.removeprefix("0x").lower()
            if len(raw) == 40:
                raw = "41" + raw
            if len(raw) != 42 or not raw.startswith("41") or not re.fullmatch(r"[0-9a-f]{42}", raw):
                raise ValueError("Geçersiz TRON hexadecimal adresi.")
        return ("0x" if prefix else "") + raw

    def address_from_hex(self, value: str) -> str:
        if self.family != "tron":
            return self.normalize_address(value)
        raw = value.removeprefix("0x").lower()
        if len(raw) == 40:
            raw = "41" + raw
        if len(raw) != 42 or not raw.startswith("41") or not re.fullmatch(r"[0-9a-f]{42}", raw):
            raise ValueError("Geçersiz TRON hexadecimal adresi.")
        return base58.b58encode_check(bytes.fromhex(raw)).decode("ascii")

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
    "tron": NetworkConfig(
        name="tron",
        display_name="TRON",
        chain_id=0,
        address_regex=re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"),
        tx_hash_regex=re.compile(r"^[a-fA-F0-9]{64}$"),
        official_usdt_contract="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        tx_explorer_url_template="https://tronscan.org/#/transaction/{value}",
        address_explorer_url_template="https://tronscan.org/#/address/{value}",
        token_explorer_url_template="https://tronscan.org/#/token20/{value}",
        default_token_decimals=6,
        family="tron",
    ),
}


def get_enabled_networks(raw: str | None = None) -> dict[str, NetworkConfig]:
    import os
    values = (raw if raw is not None else os.getenv("ENABLED_NETWORKS", "ethereum")).split(",")
    names = {value.strip().lower() for value in values if value.strip()}
    return {name: config for name, config in NETWORKS.items() if name in names}


ENABLED_NETWORKS = get_enabled_networks()


def get_network_config(name: str) -> NetworkConfig:
    normalized = name.strip().lower()
    try:
        return NETWORKS[normalized]
    except KeyError as exc:
        raise UnsupportedNetwork(normalized) from exc
