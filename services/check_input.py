"""Argument parsing for the shared /check request format."""

from __future__ import annotations

from dataclasses import dataclass

from networks import NETWORKS, get_network_config


MAX_CHECK_INPUT_LENGTH = 200


class InvalidCheckInput(ValueError):
    pass


@dataclass(frozen=True)
class CheckRequest:
    network: str
    value: str
    kind: str


def parse_check_args(args: list[str]) -> CheckRequest:
    if len(args) == 1:
        value = args[0]
        if value.startswith("T") and len(value) == 34 and get_network_config("tron").is_valid_address(value):
            network = "tron"
        elif len(value.removeprefix("0x")) == 64 and not value.lower().startswith("0x"):
            network = "tron"
        else:
            network = "ethereum"
    elif len(args) == 2:
        network, value = args[0], args[1]
        if network.strip().lower() not in NETWORKS:
            # In the two-argument form the first value is unambiguously a
            # requested network; reject unknown names without echoing them.
            get_network_config(network)
    else:
        raise InvalidCheckInput("Kullanım bilgisi")
    if len(value) > MAX_CHECK_INPUT_LENGTH:
        raise InvalidCheckInput("Kullanım bilgisi")
    config = get_network_config(network)
    normalized_hash = config.normalize_tx_hash(value)
    normalized_address = config.normalize_address(value)
    if config.is_valid_tx_hash(value):
        return CheckRequest(config.name, normalized_hash, "transaction")
    if config.is_valid_address(value):
        return CheckRequest(config.name, normalized_address, "token")
    raise InvalidCheckInput("Kullanım bilgisi")
