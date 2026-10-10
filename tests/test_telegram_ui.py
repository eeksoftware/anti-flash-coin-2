import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser

from analyzer.token_analyzer import TokenAnalysis
from models.transaction import TokenTransfer
from monitor.wallet_store import WalletSubscription
from notifications.telegram import TelegramBotController
from notifications.telegram_formatter import (
    FormattedMessage,
    format_about,
    format_add_expired,
    format_add_invalid,
    format_add_network,
    format_help,
    format_add_prompt,
    format_api_status,
    format_error,
    format_group_start,
    format_history,
    format_history_empty,
    format_history_selection,
    format_remove_confirmation,
    format_remove_result,
    format_start,
    format_stale_menu,
    format_status,
    format_transfer_alert,
    format_wallet_list,
    format_watch_error,
    format_watch_result,
    _direction_row,
    _history_entry,
    _party_rows,
    _time_row,
)
from notifications.telegram_keyboards import (
    MENU_MAIN,
    WALLET_HISTORY_PREFIX,
    WALLET_REMOVE_CONFIRM_PREFIX,
    add_network_keyboard,
    back_to_main_keyboard,
    cancel_keyboard,
    history_result_keyboard,
    history_wallets_keyboard,
    main_menu_keyboard,
    private_chat_keyboard,
    remove_confirmation_keyboard,
    status_keyboard,
    wallets_keyboard,
)


WALLET_A = "0x" + "1" * 40
WALLET_B = "0x" + "2" * 40
TX_HASH = "0x" + "a" * 64
TOKEN = "0x" + "b" * 40


def make_transfer(
    *,
    symbol: str = "USDT",
    name: str = "Tether USD",
    tx_hash: str = TX_HASH,
    sender: str = WALLET_A,
    receiver: str = WALLET_B,
    amount: str = "12.34",
) -> TokenTransfer:
    return TokenTransfer(
        network="ethereum",
        tx_hash=tx_hash,
        log_index=1,
        block_number=123,
        timestamp=datetime(2026, 10, 9, 9, 10, tzinfo=timezone.utc),
        token_address=TOKEN,
        token_symbol=symbol,
        token_name=name,
        decimals=6,
        amount=Decimal(amount),
        sender=sender,
        receiver=receiver,
        direction="RECEIVE",
    )


def subscriptions(count: int = 1, user_id: str = "123") -> list[WalletSubscription]:
    return [
        WalletSubscription(user_id, "ethereum", "0x" + f"{index:040x}")
        for index in range(1, count + 1)
    ]


def callback_data(keyboard):
    for row in keyboard.get("inline_keyboard", []):
        for button in row:
            if "callback_data" in button:
                yield button["callback_data"]


class _TagParser(HTMLParser):
    ALLOWED = {"b", "i", "u", "s", "span", "a", "code", "pre", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        assert tag in self.ALLOWED
        if tag != "br":
            self.tags.append(tag)

    def handle_endtag(self, tag):
        assert self.tags, f"unexpected closing tag: {tag}"
        assert self.tags.pop() == tag


def assert_balanced_html(text: str) -> None:
    parser = _TagParser()
    parser.feed(text)
    parser.close()
    assert not parser.tags


def test_attacker_controlled_token_symbol_is_sanitized_and_escaped():
    message = format_transfer_alert(
        make_transfer(symbol="<b>x</b>&"),
        TokenAnalysis("CANONICAL"),
    )
    assert "&lt;b&gt;x&lt;/b&gt;&amp;" in message.text
    assert "<code><b>x</b>" not in message.text
    assert_balanced_html(message.text)


def test_help_escapes_angle_bracket_placeholders():
    message = format_help()
    assert "&lt;ağ&gt;" in message.text
    assert "&lt;adres&gt;" in message.text
    assert "━━━━━━━━━━━━━━━━━━━━" in message.text
    assert_balanced_html(message.text)


def test_about_uses_requested_copy_and_shared_separator():
    message = format_about()
    assert message.text == (
        "ℹ️ <b>Hakkında</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🛡️ Bu bot, Ethereum cüzdanlarındaki token transferlerini senin yerine takip eder.\n\n"
        '🔍 Her transfer, göründüğü gibi değil gerçekte ne olduğuna göre incelenir. Sahte ya da "flash" olduğundan şüphelenilen bir token fark edildiğinde sana anında haber verir.\n\n'
        "🔔 Tek yapman gereken cüzdanını eklemek. Gerisini bot halleder.\n\n"
        "⚠️ Bot bir yardımcıdır. Yüksek tutarlı işlemlerde son kontrolü her zaman kendin yap."
    )


def test_add_address_prompt_keeps_cancel_button_available():
    message = format_add_prompt("ethereum")
    assert list(callback_data(message.reply_markup)) == ["w:cancel"]


def test_start_escapes_first_name():
    message = format_start("Anti Flash", "<b>Ada & Bob</b>", 3)
    assert "&lt;b&gt;Ada &amp; Bob&lt;/b&gt;" in message.text
    assert_balanced_html(message.text)


def test_transfer_message_includes_full_data_and_explorer_buttons():
    transfer = make_transfer()
    message = format_transfer_alert(transfer, TokenAnalysis("CANONICAL", "USDT"))
    assert transfer.sender in message.text
    assert transfer.receiver in message.text
    assert transfer.token_address in message.text
    assert transfer.tx_hash in message.text
    assert f"<code>{transfer.token_symbol}</code>" in message.text
    buttons = [button for row in message.reply_markup["inline_keyboard"] for button in row]
    assert any(button.get("url", "").endswith(f"/tx/{transfer.tx_hash}") for button in buttons)
    assert any(button.get("url", "").endswith(f"/token/{transfer.token_address}") for button in buttons)
    assert_balanced_html(message.text)


def test_transfer_and_history_share_direction_time_and_party_rows():
    transfer = make_transfer()
    alert = format_transfer_alert(transfer, TokenAnalysis("CANONICAL")).text
    history = _history_entry(transfer, Decimal("1.00"))

    shared_rows = (_time_row(transfer), _direction_row(transfer), *_party_rows(transfer))
    assert _direction_row(transfer) == "⬇️ <b>Alındı</b>"
    assert _direction_row(replace(transfer, direction="SEND")) == "⬆️ <b>Gönderildi</b>"
    assert all(row in alert and row in history for row in shared_rows)
    assert "🪙 Miktar: <b>12.34 <code>USDT</code></b>" in alert
    assert "🪙 Miktar: <b>12.34 <code>USDT</code></b>" in history
    assert "💵 USD değeri: <b>$12.34</b>" in history
    assert "09.10.2026 ·" not in history
    assert "ALINDI" not in history
    assert "GÖNDERİLDİ" not in history


def test_party_addresses_are_inline_code_and_html_escaped_in_both_messages():
    transfer = make_transfer(sender="<from&>", receiver="<to&>")
    alert = format_transfer_alert(transfer, TokenAnalysis("CANONICAL")).text
    history = _history_entry(transfer, None)

    expected_sender = "📤 Gönderen: <code>&lt;from&amp;&gt;</code>"
    expected_receiver = "📥 Alıcı: <code>&lt;to&amp;&gt;</code>"
    for text in (alert, history):
        assert expected_sender in text
        assert expected_receiver in text
        assert "<blockquote>" not in text
        assert_balanced_html(text)


def test_history_without_usd_price_omits_usd_row():
    history = _history_entry(make_transfer(), None)
    assert "💵 USD değeri:" not in history


def test_no_user_facing_formatter_message_contains_direction_label():
    wallets = subscriptions()
    messages = [
        format_start("Bot", "Ada", 1),
        format_group_start("sample_bot"),
        format_help(),
        format_about(),
        format_status(wallets),
        format_wallet_list(wallets),
        format_add_network(),
        format_add_prompt("ethereum"),
        format_add_invalid(),
        format_add_expired(),
        format_watch_result(WALLET_A, True),
        format_watch_error("Geçersiz cüzdan"),
        format_remove_confirmation(WALLET_A),
        format_remove_result(WALLET_A, True),
        format_stale_menu(),
        format_transfer_alert(make_transfer(), TokenAnalysis("CANONICAL")),
        *format_history([make_transfer()], {}, WALLET_A, history_result_keyboard(WALLET_A)),
        format_history_empty(WALLET_A, history_result_keyboard(WALLET_A)),
        format_history_selection(wallets),
        format_error("Bir hata oluştu"),
        format_api_status("API_UNAVAILABLE"),
        format_api_status("API_RECOVERED"),
    ]
    assert all("Yön" not in message.text for message in messages)


def test_long_history_is_split_under_telegram_limit_with_balanced_html():
    transfers = [
        make_transfer(tx_hash="0x" + f"{index:064x}")
        for index in range(30)
    ]
    messages = format_history(
        transfers,
        {TOKEN: Decimal("1.00")},
        WALLET_A,
        history_result_keyboard(WALLET_A),
    )
    assert len(messages) > 1
    for message in messages:
        assert len(message.text.encode("utf-16-le")) // 2 <= 4096
        assert_balanced_html(message.text)
    assert messages[-1].reply_markup is not None
    assert all(message.reply_markup is None for message in messages[:-1])


def test_unknown_classification_uses_white_fallback():
    message = format_transfer_alert(make_transfer(), TokenAnalysis("UNEXPECTED"))
    assert "⚪" in message.text
    assert "Bilinmeyen token" in message.text


def test_all_inline_callback_data_values_fit_telegram_limit():
    wallets = subscriptions(12)
    keyboards = [
        main_menu_keyboard(),
        private_chat_keyboard("sample_bot"),
        add_network_keyboard(),
        wallets_keyboard(wallets, page=0),
        wallets_keyboard(wallets, page=1),
        history_wallets_keyboard(wallets),
        history_result_keyboard(WALLET_A),
        status_keyboard(),
        back_to_main_keyboard(),
        remove_confirmation_keyboard(WALLET_A),
        cancel_keyboard(),
        format_start("Bot", "Kullanıcı", 12).reply_markup,
        format_wallet_list(wallets).reply_markup,
        format_status(wallets).reply_markup,
        format_about().reply_markup,
        format_add_network().reply_markup,
        format_stale_menu().reply_markup,
        format_watch_result(WALLET_A, True).reply_markup,
    ]
    for keyboard in keyboards:
        if keyboard is None:
            continue
        assert all(len(value.encode("utf-8")) <= 64 for value in callback_data(keyboard))


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.answered = []

    async def send_formatted(self, message, chat_id):
        self.sent.append((chat_id, message))

    async def edit_formatted(self, chat_id, message_id, message):
        self.edited.append((chat_id, message_id, message))

    async def answer_callback_query(self, callback_id, text=None, show_alert=False):
        self.answered.append((callback_id, text, show_alert))


class FakeStore:
    def __init__(self, records=None):
        self.records = records or {}
        self.removals = []

    def for_user(self, user_id):
        return list(self.records.get(user_id, []))

    def remove_wallet(self, user_id, network, address):
        self.removals.append((user_id, network, address))
        return False


class Noop:
    pass


def make_controller(store=None, notifier=None):
    return TelegramBotController(
        bot_token="token",
        monitor=Noop(),
        notifier=notifier or FakeNotifier(),
        wallet_store=store or FakeStore(),
        price_client=Noop(),
    )


def test_unknown_callback_is_handled_as_stale_menu_without_error():
    notifier = FakeNotifier()
    controller = make_controller(notifier=notifier)
    callback = {
        "id": "query-1",
        "data": "obsolete:callback",
        "from": {"id": 321, "first_name": "Test"},
        "message": {
            "message_id": 10,
            "chat": {"id": 321, "type": "private"},
        },
    }
    asyncio.run(controller._handle_callback_query(callback))
    assert notifier.answered[0][0] == "query-1"
    assert notifier.edited[0][2].text.startswith("⚠️ <b>Bu menü eski</b>")


def test_group_start_only_sends_private_bot_url_button():
    notifier = FakeNotifier()
    controller = make_controller(notifier=notifier)
    controller.bot_username = "sample_bot"
    message = {
        "text": "/start",
        "from": {"id": 100, "first_name": "Ada"},
        "chat": {"id": -1000, "type": "supergroup"},
    }
    asyncio.run(controller._handle_message(message))
    assert len(notifier.sent) == 1
    sent = notifier.sent[0][1]
    assert sent.reply_markup["inline_keyboard"] == [[{
        "text": "🤖 Botu özel mesajda aç",
        "url": "https://t.me/sample_bot?start=menu",
    }]]


def test_callback_from_another_user_cannot_remove_wallet():
    address = WALLET_A
    store = FakeStore({"alice": [WalletSubscription("alice", "ethereum", address)]})
    notifier = FakeNotifier()
    controller = make_controller(store=store, notifier=notifier)
    asyncio.run(controller._dispatch_callback(
        f"{WALLET_REMOVE_CONFIRM_PREFIX}{address}",
        "bob",
        "bob",
        10,
        {"id": "bob", "first_name": "Bob"},
    ))
    assert store.removals == []
    assert notifier.edited[0][2].text.startswith("⚠️ <b>Bu menü eski</b>")


def test_callback_from_another_user_cannot_fetch_wallet_history():
    address = WALLET_A
    store = FakeStore({"alice": [WalletSubscription("alice", "ethereum", address)]})
    controller = make_controller(store=store)

    class Monitor:
        calls = []

        async def get_recent_transfers(self, *args):
            self.calls.append(args)
            return []

    monitor = Monitor()
    controller.monitor = monitor
    asyncio.run(controller._dispatch_callback(
        f"{WALLET_HISTORY_PREFIX}{address}",
        "bob",
        "bob",
        10,
        {"id": "bob", "first_name": "Bob"},
    ))
    assert monitor.calls == []


def test_history_refresh_callback_is_defined_and_short():
    data = f"{WALLET_HISTORY_PREFIX}{WALLET_A}"
    assert len(data.encode("utf-8")) <= 64
    assert MENU_MAIN == "menu:main"
