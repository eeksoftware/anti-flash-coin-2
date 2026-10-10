"""Address poisoning heuristics.

Only addresses of the same network are compared. EVM addresses are
compared in lowercase hex; TRON Base58 addresses are compared as written.
A lookalike finding is a suspicion, never a verdict on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from analyzer.risk_engine import Signal, make_signal
from networks import NetworkConfig, get_network_config


PREFIX_MIN = 4
SUFFIX_MIN = 4
RATIO_MIN = 0.60
DUST_AMOUNT = Decimal("0.001")


@dataclass(frozen=True)
class Similarity:
    prefix: int
    suffix: int
    ratio: float
    middle_differs: bool

    @property
    def lookalike(self) -> bool:
        return (self.prefix >= PREFIX_MIN and self.suffix >= SUFFIX_MIN) or self.ratio >= RATIO_MIN


@dataclass(frozen=True)
class PoisoningFinding:
    network: str
    event_key: str
    counterparty: str
    known_address: str
    similarity: Similarity
    signals: tuple[Signal, ...]


def compare_key(config: NetworkConfig, address: str) -> str | None:
    """Return the comparison form of an address, or None when it is invalid."""
    if not isinstance(address, str) or not config.is_valid_address(address):
        return None
    return config.normalize_address(address)


def similarity(left: str, right: str) -> Similarity:
    """Compare two already-normalized addresses of the same network."""
    body_left = left[2:] if left.lower().startswith("0x") else left
    body_right = right[2:] if right.lower().startswith("0x") else right
    prefix = 0
    for a, b in zip(body_left, body_right):
        if a != b:
            break
        prefix += 1
    suffix = 0
    for a, b in zip(reversed(body_left), reversed(body_right)):
        if a != b:
            break
        suffix += 1
    if len(body_left) == len(body_right) and body_left:
        matches = sum(1 for a, b in zip(body_left, body_right) if a == b)
        ratio = matches / len(body_left)
    else:
        ratio = 0.0
    full_length = min(len(body_left), len(body_right))
    middle_differs = prefix + suffix < full_length and prefix > 0 and suffix > 0
    return Similarity(prefix, suffix, round(ratio, 4), middle_differs)


def evaluate(
    network: str,
    wallet_address: str,
    counterparty: str,
    amount: Decimal,
    known_counterparties: list[str] | tuple[str, ...],
    *,
    event_key: str = "",
) -> PoisoningFinding | None:
    """Return a finding when counterparty resembles a known address.

    known_counterparties are addresses this wallet has transacted with
    before, on this network. Without history there is nothing to compare.
    """
    config = get_network_config(network)
    wallet = compare_key(config, wallet_address)
    candidate = compare_key(config, counterparty)
    if wallet is None or candidate is None or candidate == wallet:
        return None
    best: tuple[str, Similarity] | None = None
    first_contact = True
    for known in known_counterparties:
        known_key = compare_key(config, known)
        if known_key is None or known_key == wallet:
            continue
        if known_key == candidate:
            first_contact = False
            continue
        measured = similarity(candidate, known_key)
        if not measured.lookalike:
            continue
        if best is None or (measured.ratio, measured.prefix + measured.suffix) > (best[1].ratio, best[1].prefix + best[1].suffix):
            best = (known_key, measured)
    if best is None:
        return None
    known_key, measured = best
    signals = [make_signal(
        "address_lookalike",
        f"Adres, daha önce etkileşim kurulan {_short(known_key)} adresine benziyor "
        f"(baş {measured.prefix}, son {measured.suffix}, benzerlik %{measured.ratio * 100:.0f}).",
        verified=False,
        source="address_history",
        network=config.name,
        event_key=event_key,
    )]
    if first_contact:
        signals.append(make_signal(
            "lookalike_first_contact",
            "Benzer adresle bu cüzdanın ilk etkileşimi.",
            verified=False,
            source="address_history",
            network=config.name,
            event_key=event_key,
        ))
    if amount <= DUST_AMOUNT:
        signals.append(make_signal(
            "lookalike_dust_amount",
            f"Transfer tutarı çok düşük ({format(amount, 'f')}).",
            verified=False,
            source="transfer_amount",
            network=config.name,
            event_key=event_key,
        ))
    return PoisoningFinding(config.name, event_key, candidate, known_key, measured, tuple(signals))


def _short(address: str) -> str:
    if len(address) <= 12:
        return address
    return f"{address[:6]}…{address[-4:]}"
