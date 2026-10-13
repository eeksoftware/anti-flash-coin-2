"""Decide when a transfer is small enough to leave the normal alert path.

The cutoff is 0.01 USD. Official USD stable contracts are priced at 1.
A CANONICAL USDT, USDC, or EURC uses the same unit price because the
analyzer already matched the registry. Every other token needs a market
price; a missing price stays on the normal alert path.
"""

from decimal import Decimal


FEE_USD = Decimal("0.01")

_USD_STABLES = {
    ("ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7"),
    ("ethereum", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"),
    ("tron", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"),
}
_CANONICAL_UNITS = frozenset({"USDT", "USDC", "EURC"})


def stable_unit_price(network: str, token_address: str, classification: str, symbol: str) -> Decimal | None:
    """Return 1 for a known USD unit, otherwise None so a market price can be used."""
    key = (network, token_address if network == "tron" else str(token_address or "").lower())
    if key in _USD_STABLES:
        return Decimal(1)
    if classification == "CANONICAL" and symbol in _CANONICAL_UNITS:
        return Decimal(1)
    return None


def quote_usd(amount: Decimal, price: Decimal | None) -> Decimal | None:
    if price is None:
        return None
    value = amount * price
    if not value.is_finite() or value < 0:
        return None
    return value


def fee_action(usd: Decimal | None, *, suspicious: bool, notify_fees: bool) -> str | None:
    """Return skip, fee, or None.

    None keeps the normal alert. skip drops the Telegram message.
    fee sends the short fee notice. Suspicious transfers never take this path.
    """
    if suspicious or usd is None or usd >= FEE_USD:
        return None
    return "fee" if notify_fees else "skip"
