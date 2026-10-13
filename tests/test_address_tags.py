from datetime import datetime, timezone
from decimal import Decimal

from analyzer.token_analyzer import TokenAnalysis
from blockchain.etherscan import parse_address_tags
from models.transaction import TokenTransfer
from notifications.telegram_formatter import format_transfer_alert


SENDER = "0x80d04079ffc53eb14edbeeb37279d126f45552df"


def test_address_tags_keep_the_public_name_only():
    names = parse_address_tags({
        "status": "1",
        "result": [{
            "address": SENDER,
            "nametag": " Fake_Phishing3348326\n<script> ",
            "labels": ["Phish / Hack"],
            "notes_1": "secret note",
        }],
    })
    assert names == {SENDER: "Fake_Phishing3348326<script>"}
    assert parse_address_tags({"status": "0", "result": "missing plan"}) == {}


def test_transfer_alert_shows_the_public_name_escaped():
    transfer = TokenTransfer(
        network="ethereum", tx_hash="0x" + "ab" * 32, log_index=1, block_number=1,
        timestamp=datetime(2026, 10, 10, tzinfo=timezone.utc),
        token_address="0x" + "cd" * 20, token_symbol="USDT", token_name="Tether",
        decimals=6, amount=Decimal("1"), sender=SENDER, receiver="0x" + "22" * 20,
        direction="RECEIVE",
    )
    message = format_transfer_alert(
        transfer,
        TokenAnalysis("UNKNOWN", "USDT"),
        address_names={SENDER: "Fake_Phishing3348326 <b>"},
    )
    assert "Fake_Phishing3348326 &lt;b&gt;" in message.text
    assert "<b>Fake_Phishing3348326 <b>" not in message.text
    assert f"<code>{SENDER}</code>" in message.text
