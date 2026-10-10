"""ERC-20 Approval event parsing and assessment.

Only EVM networks are supported. Raw allowance values stay integers;
the human-readable value is derived from decimals only for display.
Unlimited means a raw value at or above 2**255, which covers the common
type(uint256).max pattern. A large allowance is only a weak signal.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from analyzer.risk_engine import Signal, make_signal
from networks import get_network_config


APPROVAL_TOPIC = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
UNLIMITED_FLOOR = 2 ** 255
LARGE_HUMAN_AMOUNT = Decimal("1000000")
logger = logging.getLogger(__name__)


class UnsupportedApprovalNetwork(ValueError):
    """Raised when a network has no approval data source."""


@dataclass(frozen=True)
class ApprovalEvent:
    network: str
    tx_hash: str
    block_number: int
    log_index: int
    token_address: str
    owner: str
    spender: str
    value_raw: int

    @property
    def event_key(self) -> str:
        return json.dumps([self.network, self.tx_hash.lower(), f"log:{self.log_index}", "approval"], separators=(",", ":"))


@dataclass(frozen=True)
class ApprovalAssessment:
    state: str
    human_value: Decimal | None
    previous_raw: int | None
    signals: tuple[Signal, ...]
    spender_label: str | None = None


def parse_approval_log(raw: dict[str, Any], network: str = "ethereum") -> ApprovalEvent | None:
    """Convert one Etherscan getLogs row into an ApprovalEvent; None when malformed."""
    config = get_network_config(network)
    if config.family != "evm" or not isinstance(raw, dict):
        return None
    topics = raw.get("topics")
    if not isinstance(topics, list) or len(topics) != 3 or str(topics[0]).lower() != APPROVAL_TOPIC:
        return None
    owner = _topic_address(topics[1])
    spender = _topic_address(topics[2])
    token = str(raw.get("address") or "").lower()
    data = raw.get("data")
    tx_hash = str(raw.get("transactionHash") or "").lower()
    if not owner or not spender or not config.is_valid_address(token) or not isinstance(data, str) or not config.is_valid_tx_hash(tx_hash):
        return None
    try:
        value_raw = int(data, 16) if data not in ("0x", "") else 0
        block_number = _hex_int(raw.get("blockNumber"))
        log_index = _hex_int(raw.get("logIndex"))
    except (TypeError, ValueError):
        return None
    if value_raw < 0 or block_number < 0 or log_index < 0:
        return None
    return ApprovalEvent(config.name, tx_hash, block_number, log_index, token, owner, spender, value_raw)


def human_amount(value_raw: int, decimals: int | None) -> Decimal | None:
    if decimals is None or not 0 <= decimals <= 255:
        return None
    return Decimal(f"{value_raw}e-{decimals}")


class SpenderRegistry:
    """Known spender labels from registry files. Empty lists are valid."""

    def __init__(self, registry_dir: Path | None = None) -> None:
        base = registry_dir or Path(__file__).resolve().parents[1] / "registry"
        self.fraud_contracts: dict[tuple[str, str], dict[str, Any]] = {}
        self.risky: dict[tuple[str, str], dict[str, Any]] = {}
        self.trusted: dict[tuple[str, str], dict[str, Any]] = {}
        self._load_fraud_tokens(base / "known_fraud_tokens.json")
        self._load_spenders(base / "known_spenders.json")

    def _load_fraud_tokens(self, path: Path) -> None:
        for item in _read_json(path).get("known_fraud_tokens", []):
            key = _registry_key(item.get("network"), item.get("contract"))
            if key is not None:
                self.fraud_contracts[key] = item

    def _load_spenders(self, path: Path) -> None:
        data = _read_json(path)
        for bucket, target in (("risky", self.risky), ("trusted", self.trusted)):
            for item in data.get(bucket, []):
                if not isinstance(item, dict) or not item.get("source"):
                    continue
                key = _registry_key(item.get("network"), item.get("address"))
                if key is not None:
                    target[key] = item

    def lookup(self, network: str, spender: str) -> tuple[str, dict[str, Any]] | None:
        key = _registry_key(network, spender)
        if key is None:
            return None
        if key in self.fraud_contracts:
            return "fraud", self.fraud_contracts[key]
        if key in self.risky:
            return "risky", self.risky[key]
        if key in self.trusted:
            return "trusted", self.trusted[key]
        return None


def assess(
    event: ApprovalEvent,
    previous_raw: int | None,
    decimals: int | None,
    registry: SpenderRegistry | None = None,
) -> ApprovalAssessment:
    config = get_network_config(event.network)
    if config.family != "evm":
        raise UnsupportedApprovalNetwork(f"Approval izleme bu ağda desteklenmiyor: {config.display_name}")
    value = event.value_raw
    human = human_amount(value, decimals)
    signals: list[Signal] = []
    key = event.event_key
    if value == 0:
        state = "REVOKED"
    elif value >= UNLIMITED_FLOOR:
        state = "UNLIMITED"
        signals.append(make_signal("unlimited_allowance", "Harcama yetkisi sınırsız (2^255 ve üzeri).", source="onchain", network=event.network, event_key=key))
    elif previous_raw is None:
        state = "NEW"
        signals.append(make_signal("new_allowance", "Bu harcayıcıya ilk kez yetki verildi.", source="onchain", network=event.network, event_key=key))
    elif value > previous_raw:
        state = "INCREASED"
        signals.append(make_signal("allowance_increase", "Mevcut harcama yetkisi artırıldı.", source="onchain", network=event.network, event_key=key))
    else:
        state = "DECREASED"
    if state in {"NEW", "INCREASED"} and human is not None and human >= LARGE_HUMAN_AMOUNT:
        signals.append(make_signal("large_allowance", f"Yetki tutarı yüksek ({format(human, 'f')}).", source="onchain", network=event.network, event_key=key))
    label = None
    if registry is not None and value > 0:
        match = registry.lookup(event.network, event.spender)
        if match is not None:
            kind, item = match
            label = str(item.get("label") or item.get("symbol") or kind)
            if kind == "fraud":
                signals.append(make_signal("known_fraud_spender", "Harcayıcı bilinen sahte sözleşme listesinde.", source="registry", network=event.network, event_key=key))
            elif kind == "risky":
                signals.append(make_signal("listed_risky_spender", f"Harcayıcı riskli listede ({item.get('source')}).", source="registry", network=event.network, event_key=key))
    return ApprovalAssessment(state, human, previous_raw, tuple(signals), label)


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


def _hex_int(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.lower().startswith("0x") else int(value)
    raise ValueError("Geçersiz hexadecimal sayı")


def _registry_key(network: Any, address: Any) -> tuple[str, str] | None:
    if not isinstance(network, str) or not isinstance(address, str):
        return None
    try:
        config = get_network_config(network)
    except ValueError:
        return None
    if not config.is_valid_address(address):
        return None
    return config.name, config.normalize_address(address)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        logger.warning("Kayıt dosyası okunamadı: %s", path.name)
        return {}
    return data if isinstance(data, dict) else {}
