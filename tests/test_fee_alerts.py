from datetime import datetime, timezone
from decimal import Decimal

from models.transaction import TokenTransfer
from monitor.state_db import UserStateStore, WalletSettings
from notifications.fee_alerts import fee_action, quote_usd, stable_unit_price
from notifications.telegram_formatter import format_fee_alert


USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
TRON_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def _transfer(amount="0.000172", symbol="USDT", address=USDT, classification_symbol="USDT"):
    return TokenTransfer(
        network="ethereum",
        tx_hash="0x" + "ab" * 32,
        log_index=1,
        block_number=1,
        timestamp=NOW,
        token_address=address,
        token_symbol=symbol,
        token_name=classification_symbol,
        decimals=6,
        amount=Decimal(amount),
        sender="0x" + "11" * 20,
        receiver="0x" + "22" * 20,
        direction="RECEIVE",
    )


def test_official_stables_are_one_dollar_and_lookalikes_are_not():
    assert stable_unit_price("ethereum", USDT, "CANONICAL", "USDT") == Decimal(1)
    assert stable_unit_price("ethereum", USDC.upper(), "CANONICAL", "USDC") == Decimal(1)
    assert stable_unit_price("tron", TRON_USDT, "CANONICAL", "USDT") == Decimal(1)
    assert stable_unit_price("ethereum", "0x" + "cd" * 20, "UNKNOWN", "USDT") is None
    assert stable_unit_price("ethereum", "0x" + "cd" * 20, "CANONICAL", "EURC") == Decimal(1)


def test_fee_action_skips_unless_the_wallet_opts_in_and_never_hides_suspicious():
    dust = quote_usd(Decimal("0.000172"), Decimal(1))
    real = quote_usd(Decimal("12.5"), Decimal(1))
    assert fee_action(dust, suspicious=False, notify_fees=False) == "skip"
    assert fee_action(dust, suspicious=False, notify_fees=True) == "fee"
    assert fee_action(real, suspicious=False, notify_fees=False) is None
    assert fee_action(dust, suspicious=True, notify_fees=False) is None
    assert fee_action(None, suspicious=False, notify_fees=False) is None
    assert quote_usd(Decimal("1"), None) is None


def test_fee_message_is_short_and_has_no_feedback_buttons():
    message = format_fee_alert(_transfer(), Decimal("0.000172"), alias="Ada")
    assert "💸 <b>Fee</b>" in message.text
    assert "Sınıflandırma" not in message.text
    assert "Ada" in message.text
    assert "&lt;$0.01" in message.text or "<$0.01" in message.text
    buttons = [button for row in message.reply_markup["inline_keyboard"] for button in row]
    assert buttons
    assert all("callback_data" not in button for button in buttons)


def test_fee_preference_round_trip(tmp_path):
    store = UserStateStore(tmp_path / "user_state.sqlite")
    wallet = "0x" + "aa" * 20
    store.save_settings("7", "ethereum", wallet, WalletSettings(fee_alerts=True, alias="Ada"))
    saved = store.settings("7", "ethereum", wallet)
    assert saved.fee_alerts is True
    assert saved.alias == "Ada"
    store.save_settings("7", "ethereum", wallet, WalletSettings(alias="Ada"))
    assert store.settings("7", "ethereum", wallet).fee_alerts is False
    store.close()
