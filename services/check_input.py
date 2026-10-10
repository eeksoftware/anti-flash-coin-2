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
        network, value = "ethereum", args[0]
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
    normalized = value.lower()
    if config.tx_hash_regex.fullmatch(normalized):
        return CheckRequest(config.name, normalized, "transaction")
    if config.address_regex.fullmatch(normalized):
        return CheckRequest(config.name, normalized, "token")
    raise InvalidCheckInput("Kullanım bilgisi")
