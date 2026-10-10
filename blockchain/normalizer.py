from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from models.transaction import TokenTransfer


def _parse_int(value: Any, field_name: str) -> int:
    """Etherscan'dan gelen tamsayı alanını kayan nokta kullanmadan ayrıştır."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"Geçersiz Etherscan {field_name}: tamsayı bekleniyordu.")

    try:
        if isinstance(value, str) and value.lower().startswith("0x"):
            return int(value, 16)
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Geçersiz Etherscan {field_name} değeri: {value!r}.") from exc


def normalize_etherscan_transfer(
    raw: dict[str, Any],
    wallet_address: str,
) -> TokenTransfer:
    """Ham bir Ethereum ERC-20 transfer kaydını TokenTransfer nesnesine dönüştür."""
    try:
        tx_hash = str(raw["hash"])
        block_number = _parse_int(raw["blockNumber"], "blockNumber")
        timestamp_value = _parse_int(raw["timeStamp"], "timeStamp")
        token_address = str(raw["contractAddress"])
        token_symbol = str(raw["tokenSymbol"])
        token_name = str(raw["tokenName"])
        decimals = _parse_int(raw["tokenDecimal"], "tokenDecimal")
        raw_amount = _parse_int(raw["value"], "value")
        sender = str(raw["from"])
        receiver = str(raw["to"])
    except KeyError as exc:
        raise ValueError(f"Etherscan transferinde {exc.args[0]!r} alanı eksik.") from exc

    if not tx_hash or not token_address or not sender or not receiver:
        raise ValueError("Etherscan transferinde işlem kimliği veya adres boş.")
    raw_log_index = raw.get("logIndex")
    log_index = (
        _parse_int(raw_log_index, "logIndex")
        if raw_log_index not in (None, "")
        else -1
    )

    if block_number < 0 or timestamp_value < 0 or decimals < 0:
        raise ValueError("Etherscan transferinde negatif sayısal alan var.")
    if log_index < -1:
        raise ValueError("Etherscan transferindeki logIndex geçersiz.")

    wallet = wallet_address.lower()
    if receiver.lower() == wallet:
        direction = "RECEIVE"
    elif sender.lower() == wallet:
        direction = "SEND"
    else:
        raise ValueError("Etherscan transferinde izlenen cüzdan yer almıyor.")

    # Decimal string construction is exact even for large uint256 values.
    amount = Decimal(f"{raw_amount}e-{decimals}")
    event_key = (
        f"log:{log_index}"
        if log_index >= 0
        else "transfer:{}:{}:{}:{}:{}".format(
            token_address.lower(), sender.lower(), receiver.lower(), raw_amount, decimals
        )
    )

    return TokenTransfer(
        network="ethereum",
        tx_hash=tx_hash,
        log_index=log_index,
        block_number=block_number,
        timestamp=datetime.fromtimestamp(timestamp_value, tz=timezone.utc),
        token_address=token_address.lower(),
        token_symbol=token_symbol,
        token_name=token_name,
        decimals=decimals,
        amount=amount,
        sender=sender.lower(),
        receiver=receiver.lower(),
        direction=direction,
        event_key=event_key,
    )


def normalize_etherscan_transfers(
    records: list[dict[str, Any]],
    wallet_address: str,
) -> list[TokenTransfer]:
    """Ham Ethereum ERC-20 transfer kayıtlarını standart biçime dönüştür."""
    return [
        normalize_etherscan_transfer(record, wallet_address)
        for record in records
    ]
