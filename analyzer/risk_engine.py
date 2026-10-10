"""Risk signals and a pure 0–100 score.

The score sits beside the token classification and the user feedback. It
never rewrites either of them. Weights below are documented starting
values, not calibrated results. WEIGHTS_VERSION is stored with each score so
later weight changes stay traceable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from analyzer.token_analyzer import TokenAnalysis


WEIGHTS_VERSION = "stage7-v1"
SCORE_MIN = 0
SCORE_MAX = 100
UNVERIFIED_FACTOR = 0.5

# Each weight belongs to one detector. Verified findings come from registry
# matches or exact on-chain values; heuristic findings are marked unverified
# and are halved before summing.
WEIGHTS: dict[str, int] = {
    # token classification (registry match)
    "known_fraud_token": 60,
    "variant_token": 35,
    "unknown_token": 10,
    # address poisoning (heuristic)
    "address_lookalike": 30,
    "lookalike_first_contact": 15,
    "lookalike_dust_amount": 20,
    # approvals (on-chain value, registry match)
    "unlimited_allowance": 30,
    "large_allowance": 15,
    "allowance_increase": 10,
    "new_allowance": 5,
    "known_fraud_spender": 60,
    "listed_risky_spender": 40,
}

CATEGORIES: dict[str, str] = {
    "known_fraud_token": "classification",
    "variant_token": "classification",
    "unknown_token": "classification",
    "address_lookalike": "address_poisoning",
    "lookalike_first_contact": "address_poisoning",
    "lookalike_dust_amount": "address_poisoning",
    "unlimited_allowance": "approval",
    "large_allowance": "approval",
    "allowance_increase": "approval",
    "new_allowance": "approval",
    "known_fraud_spender": "approval",
    "listed_risky_spender": "approval",
}

# Thresholds are starting values for the thesis evaluation. They are not
# calibrated against labelled data yet.
LEVEL_THRESHOLDS: tuple[tuple[int, str], ...] = (
    (75, "KRİTİK"),
    (50, "YÜKSEK"),
    (25, "ORTA"),
    (0, "DÜŞÜK"),
)
LEVEL_ICONS = {"DÜŞÜK": "🟢", "ORTA": "🟡", "YÜKSEK": "🟠", "KRİTİK": "🔴"}


@dataclass(frozen=True)
class Signal:
    name: str
    weight: int
    evidence: str
    category: str = ""
    verified: bool = True
    source: str = ""
    network: str = ""
    event_key: str = ""

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum() or self.name != self.name.lower():
            raise ValueError("Sinyal adı küçük harf ve alt çizgiden oluşmalı.")
        if not isinstance(self.weight, int) or isinstance(self.weight, bool):
            raise ValueError("Sinyal ağırlığı tam sayı olmalı.")
        if not SCORE_MIN <= self.weight <= SCORE_MAX:
            raise ValueError("Sinyal ağırlığı 0 ile 100 arasında olmalı.")
        if not self.category:
            object.__setattr__(self, "category", CATEGORIES.get(self.name, "other"))


@dataclass(frozen=True)
class RiskScore:
    score: int
    level: str
    contributions: tuple[tuple[str, int], ...]
    missing: tuple[str, ...] = ()
    weights_version: str = WEIGHTS_VERSION
    signals: tuple[Signal, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def icon(self) -> str:
        return LEVEL_ICONS.get(self.level, "⚪")


def make_signal(name: str, evidence: str, *, verified: bool = True, source: str = "", network: str = "", event_key: str = "") -> Signal:
    return Signal(name, WEIGHTS[name], evidence, verified=verified, source=source, network=network, event_key=event_key)


def classification_signals(analysis: TokenAnalysis | None, *, network: str = "", event_key: str = "") -> list[Signal]:
    if analysis is None:
        return []
    if analysis.classification == "KNOWN_FRAUD":
        return [make_signal("known_fraud_token", "Token bilinen sahte listesinde.", source="registry", network=network, event_key=event_key)]
    if analysis.classification == "VARIANT":
        return [make_signal("variant_token", "Token kayıtlı varyant listesinde.", source="registry", network=network, event_key=event_key)]
    if analysis.classification == "UNKNOWN":
        return [make_signal("unknown_token", "Token kayıtlı değil; tek başına sahte göstermez.", verified=False, source="registry", network=network, event_key=event_key)]
    return []


def level_for(score: int) -> str:
    for threshold, name in LEVEL_THRESHOLDS:
        if score >= threshold:
            return name
    return LEVEL_THRESHOLDS[-1][1]


def compute_risk(signals: list[Signal] | tuple[Signal, ...], *, missing: tuple[str, ...] = ()) -> RiskScore:
    """Sum deduplicated signal weights and clamp to 0–100.

    Duplicate names keep only the strongest instance so repeated evidence
    for one event cannot inflate the score. Unverified signals are halved.
    missing names the inputs that were unavailable; it is reported, not
    treated as zero risk.
    """
    strongest: dict[str, Signal] = {}
    for signal in signals:
        current = strongest.get(signal.name)
        if current is None or _applied(signal) > _applied(current):
            strongest[signal.name] = signal
    contributions: list[tuple[str, int]] = []
    total = 0
    for name in sorted(strongest):
        applied = _applied(strongest[name])
        contributions.append((name, applied))
        total += applied
    score = max(SCORE_MIN, min(SCORE_MAX, total))
    return RiskScore(
        score=score,
        level=level_for(score),
        contributions=tuple(contributions),
        missing=tuple(missing),
        signals=tuple(strongest[name] for name in sorted(strongest)),
    )


def _applied(signal: Signal) -> int:
    return signal.weight if signal.verified else int(signal.weight * UNVERIFIED_FACTOR)
