import asyncio

from notifications.telegram import TelegramBotController
from notifications.telegram_formatter import format_help


class FakeNotifier:
    def __init__(self): self.sent = []; self.api_calls = []
    async def send_formatted(self, message, chat_id=None):
        self.sent.append((chat_id, message))
        return {"message_id": len(self.sent)}
    async def call_api(self, method, payload=None):
        self.api_calls.append((method, payload))
        return {"ok": True, "result": {"first_name": "Bot", "username": "example_bot"}}


def controller(admin_ids):
    notifier = FakeNotifier()
    instance = TelegramBotController("token", object(), notifier, object(), object(),
        admin_ids=admin_ids, health_reporter=lambda: {"icon": "🟢", "label": "Normal", "keys": []},
        stats_reporter=lambda: {"today": {}, "last24h": {}, "api": {}})
    return instance, notifier


def message(command, *, user=123, chat=123, chat_type="private"):
    return {"text": command, "from": {"id": user}, "chat": {"id": chat, "type": chat_type}}


def test_admin_health_command_private_only_and_private_reply():
    instance, notifier = controller({"123"})
    asyncio.run(instance._handle_message(message("/health")))
    assert len(notifier.sent) == 1
    assert "Sağlık Durumu" in notifier.sent[0][1].text
    asyncio.run(instance._handle_message(message("/health", chat=456, chat_type="group")))
    assert len(notifier.sent) == 1


def test_non_admin_is_silent_and_admin_commands_are_not_public_help():
    instance, notifier = controller({"123"})
    asyncio.run(instance._handle_message(message("/stats", user=456, chat=456)))
    assert notifier.sent == []
    help_text = format_help().text
    assert "/health" not in help_text and "/stats" not in help_text


def test_admin_commands_are_not_published_to_telegram_command_menu():
    instance, notifier = controller({"123"})
    asyncio.run(instance._configure_bot())
    payload = next(payload for method, payload in notifier.api_calls if method == "setMyCommands")
    commands = {item["command"] for item in payload["commands"]}
    assert "health" not in commands and "stats" not in commands


def test_admin_stats_command_responds_without_private_values():
    instance, notifier = controller({"123"})
    asyncio.run(instance._handle_message(message("/stats")))
    assert len(notifier.sent) == 1
    assert "İstatistikler" in notifier.sent[0][1].text
    assert "987654321" not in notifier.sent[0][1].text
