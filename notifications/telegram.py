import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from analyzer.token_analyzer import TokenAnalysis
from blockchain.token_price import DexScreenerPriceClient
from models.transaction import TokenTransfer
from networks import get_network_config
from monitor.wallet_monitor import WalletMonitorManager
from monitor.wallet_store import WalletStateStore, WalletSubscription
from notifications.telegram_formatter import (
    FormattedMessage,
    format_about,
    format_add_expired,
    format_add_invalid,
    format_add_network,
    format_add_prompt,
    format_api_status,
    format_error,
    format_group_start,
    format_help,
    format_history,
    format_history_empty,
    format_history_selection,
    format_remove_confirmation,
    format_remove_result,
    format_stale_menu,
    format_start,
    format_status,
    format_transfer_alert,
    format_wallet_list,
    format_watch_result,
)
from notifications.telegram_keyboards import (
    MENU_ABOUT,
    MENU_ADD,
    MENU_HELP,
    MENU_HISTORY,
    MENU_MAIN,
    MENU_STATUS,
    MENU_WALLETS,
    NETWORKS,
    WALLET_ADD_PREFIX,
    WALLET_CANCEL,
    WALLET_HISTORY_PREFIX,
    WALLET_HISTORY_REFRESH_PREFIX,
    WALLET_LIST_PREFIX,
    WALLET_REMOVE_CANCEL_PREFIX,
    WALLET_REMOVE_CONFIRM_PREFIX,
    WALLET_REMOVE_PREFIX,
    cancel_keyboard,
    history_result_keyboard,
    main_menu_keyboard,
)


logger = logging.getLogger(__name__)


class TelegramError(Exception):
    """Telegram API ile iletişim hatası."""


@dataclass(frozen=True)
class PendingAddress:
    network: str
    chat_id: str
    message_id: int
    expires_at: float


class TelegramNotifier:
    BASE_URL = "https://api.telegram.org"

    def __init__(self, bot_token: str, chat_id: str = "") -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self.session

    async def call_api(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        ignore_not_modified: bool = False,
    ) -> dict[str, Any]:
        session = await self._ensure_session()
        try:
            async with session.post(
                f"{self.BASE_URL}/bot{self.bot_token}/{method}",
                json=payload or {},
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except (aiohttp.ClientError, ValueError) as exc:
                    raise TelegramError("Telegram geçersiz bir yanıt döndürdü.") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise TelegramError(f"Telegram isteği başarısız: {exc}") from exc

        if not isinstance(data, dict):
            raise TelegramError("Telegram yanıtı beklenen biçimde değil.")
        if response.status >= 400 or not data.get("ok"):
            description = str(data.get("description", "Telegram isteği reddetti."))
            if ignore_not_modified and "message is not modified" in description.lower():
                return data
            raise TelegramError(description)
        return data

    async def send_formatted(
        self,
        message: FormattedMessage,
        chat_id: str | int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id or self.chat_id,
            "text": message.text,
            "parse_mode": message.parse_mode,
            "link_preview_options": {"is_disabled": True},
        }
        if message.reply_markup is not None:
            payload["reply_markup"] = message.reply_markup
        response = await self.call_api("sendMessage", payload)
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    async def send_text(
        self,
        text: str,
        chat_id: str | int | None = None,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool = True,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": disable_web_page_preview},
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self.call_api("sendMessage", payload)

    async def edit_formatted(
        self,
        chat_id: str | int,
        message_id: int,
        message: FormattedMessage,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": message.text,
            "parse_mode": message.parse_mode,
            "link_preview_options": {"is_disabled": True},
            "reply_markup": message.reply_markup or {"inline_keyboard": []},
        }
        await self.call_api("editMessageText", payload, ignore_not_modified=True)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = True
        await self.call_api("answerCallbackQuery", payload)

    async def send_transfer_alert(
        self,
        transfer: TokenTransfer,
        analysis: TokenAnalysis,
        chat_id: str | int | None = None,
    ) -> dict[str, Any]:
        return await self.send_formatted(format_transfer_alert(transfer, analysis), chat_id)

    async def send_api_status_alert(
        self,
        status: str,
        chat_id: str | int | None = None,
    ) -> None:
        await self.send_formatted(format_api_status(status), chat_id)

    async def close(self) -> None:
        if self.session is not None and not self.session.closed:
            await self.session.close()


class TelegramBotController:
    WALLET_ADDRESS_RE = get_network_config("ethereum").address_regex
    LONG_POLL_SECONDS = 25
    PAGE_SIZE = 8
    PENDING_ADDRESS_TTL = 5 * 60

    def __init__(
        self,
        bot_token: str,
        monitor: WalletMonitorManager,
        notifier: TelegramNotifier,
        wallet_store: WalletStateStore,
        price_client: DexScreenerPriceClient,
    ) -> None:
        self.bot_token = bot_token
        self.monitor = monitor
        self.notifier = notifier
        self.wallet_store = wallet_store
        self.price_client = price_client
        self.session: aiohttp.ClientSession | None = None
        self._offset: int | None = None
        self.bot_name = "Anti-Flash Token Bot"
        self.bot_username: str | None = None
        self._pending_addresses: dict[str, PendingAddress] = {}

    async def run(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.LONG_POLL_SECONDS + 10)
        )
        await self._configure_bot()
        logger.info("Telegram komut ve menü dinleyicisi başlatıldı.")
        retry_delay = 2.0
        try:
            while True:
                try:
                    await self._poll_updates()
                    retry_delay = 2.0
                except (TelegramError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    logger.warning("Telegram güncellemeleri alınamadı: %s", exc)
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 30.0)
        finally:
            await self.close()

    async def _configure_bot(self) -> None:
        try:
            identity = await self.notifier.call_api("getMe")
            bot = identity.get("result", {})
            if isinstance(bot, dict):
                self.bot_name = str(bot.get("first_name") or self.bot_name)
                username = bot.get("username")
                self.bot_username = username if isinstance(username, str) else None
        except TelegramError as exc:
            logger.warning("Bot profili alınamadı: %s", exc)

        commands = [
            {"command": "start", "description": "Ana menüyü aç"},
            {"command": "wallets", "description": "İzlenen cüzdanları listele"},
            {"command": "watch", "description": "İzlenecek cüzdan ekle"},
            {"command": "unwatch", "description": "İzlenen cüzdanı kaldır"},
            {"command": "history", "description": "Son token transferlerini göster"},
            {"command": "status", "description": "İzleme durumunu göster"},
            {"command": "help", "description": "Yardım menüsünü aç"},
        ]
        setup_calls = [
            (
                "setMyCommands",
                {"commands": commands},
                "Bot komutları ayarlandı.",
                "Bot komutları ayarlanamadı: %s",
            ),
            (
                "setMyDescription",
                {"description": "Ethereum cüzdanlarındaki token transferlerini izler, şüpheli kontratlar için uyarı verir."},
                "Bot tanıtımı ayarlandı.",
                "Bot tanıtımı ayarlanamadı: %s",
            ),
            (
                "setMyShortDescription",
                {"short_description": "Ethereum cüzdan ve token transferi izleme botu."},
                "Bot kısa tanıtımı ayarlandı.",
                "Bot kısa tanıtımı ayarlanamadı: %s",
            ),
        ]
        for method, payload, success_message, failure_message in setup_calls:
            try:
                await self.notifier.call_api(method, payload)
                logger.info(success_message)
            except TelegramError as exc:
                logger.warning(failure_message, exc)

    async def _poll_updates(self) -> None:
        assert self.session is not None
        params: dict[str, Any] = {
            "timeout": self.LONG_POLL_SECONDS,
            "allowed_updates": ["message", "callback_query"],
        }
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            async with self.session.post(
                f"{TelegramNotifier.BASE_URL}/bot{self.bot_token}/getUpdates",
                json=params,
            ) as response:
                data = await response.json(content_type=None)
                if response.status >= 400:
                    description = data.get("description", "") if isinstance(data, dict) else ""
                    raise TelegramError(f"Telegram güncelleme isteği başarısız: {description}")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise TelegramError(f"Telegram güncelleme isteği başarısız: {exc}") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            raise TelegramError("Telegram güncellemeleri başarıyla döndürmedi.")
        updates = data.get("result", [])
        if not isinstance(updates, list):
            raise TelegramError("Telegram geçersiz bir güncelleme yanıtı döndürdü.")
        if updates:
            logger.info("Telegram'dan %d güncelleme alındı.", len(updates))
        for update in updates:
            if not isinstance(update, dict):
                continue
            if isinstance(update.get("callback_query"), dict):
                await self._handle_callback_query(update["callback_query"])
            elif isinstance(update.get("message"), dict):
                await self._handle_message(update["message"])
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._offset = update_id + 1

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat")
        sender = message.get("from")
        text = message.get("text")
        if not isinstance(chat, dict) or not isinstance(sender, dict) or not isinstance(text, str):
            return
        chat_id = str(chat.get("id"))
        user_id = str(sender.get("id"))
        chat_type = chat.get("type")
        parts = text.strip().split()
        if not parts:
            return
        command = parts[0].split("@", 1)[0].lower()

        if chat_type != "private" or chat_id != user_id:
            if command == "/start":
                await self._send(chat_id, format_group_start(self.bot_username))
            else:
                logger.info("Özel sohbet dışından gelen cüzdan komutu yok sayıldı.")
            return

        if command == "/start":
            self._pending_addresses.pop(user_id, None)
            await self._send(
                chat_id,
                format_start(
                    self.bot_name,
                    str(sender.get("first_name") or "kullanıcı"),
                    len(self.wallet_store.for_user(user_id)),
                ),
            )
            return
        if command == "/help":
            await self._send(chat_id, format_help())
            return

        pending = self._pending_addresses.get(user_id)
        if pending is not None and not command.startswith("/"):
            await self._accept_pending_address(user_id, chat_id, text.strip(), pending)
            return
        if pending is not None and time.monotonic() >= pending.expires_at:
            self._pending_addresses.pop(user_id, None)
            await self._edit(pending.chat_id, pending.message_id, format_add_expired())

        if command == "/status":
            await self._send(chat_id, format_status(self.wallet_store.for_user(user_id)))
        elif command == "/wallets":
            await self._send(chat_id, format_wallet_list(self.wallet_store.for_user(user_id)))
        elif command == "/watch":
            await self._handle_watch_command(parts, user_id, chat_id)
        elif command == "/unwatch":
            await self._handle_unwatch_command(parts, user_id, chat_id)
        elif command == "/history":
            await self._handle_history_command(parts, chat_id)
        else:
            logger.info("Bilinmeyen Telegram komutu yok sayıldı.")

    async def _accept_pending_address(
        self,
        user_id: str,
        chat_id: str,
        address: str,
        pending: PendingAddress,
    ) -> None:
        if time.monotonic() >= pending.expires_at:
            self._pending_addresses.pop(user_id, None)
            await self._edit(pending.chat_id, pending.message_id, format_add_expired())
            return
        if chat_id != pending.chat_id or not self.WALLET_ADDRESS_RE.fullmatch(address):
            await self._edit(pending.chat_id, pending.message_id, format_add_invalid())
            return
        try:
            added = self.wallet_store.add_wallet(user_id, pending.network, address)
        except (OSError, ValueError) as exc:
            logger.error("İstenen cüzdan kaydedilemedi: %s", exc)
            await self._edit(
                pending.chat_id,
                pending.message_id,
                format_error("Cüzdan kaydedilemedi. Mevcut izlemeler sürüyor.", cancel_keyboard()),
            )
            return
        self._pending_addresses.pop(user_id, None)
        await self._edit(
            pending.chat_id,
            pending.message_id,
            format_watch_result(address, added),
        )

    async def _handle_watch_command(self, parts: list[str], user_id: str, chat_id: str) -> None:
        if len(parts) != 3:
            await self._send(chat_id, format_error("Kullanım: /watch <ağ> <adres>", main_menu_keyboard()))
            return
        network = parts[1].lower()
        address = parts[2]
        if network not in NETWORKS:
            await self._send(chat_id, format_error("Şu anda yalnızca Ethereum ağı destekleniyor.", main_menu_keyboard()))
            return
        if not self.WALLET_ADDRESS_RE.fullmatch(address):
            await self._send(chat_id, format_error("Geçerli bir Ethereum 0x cüzdan adresi gir.", main_menu_keyboard()))
            return
        try:
            added = self.wallet_store.add_wallet(user_id, network, address)
        except (OSError, ValueError) as exc:
            logger.error("İstenen cüzdan kaydedilemedi: %s", exc)
            await self._send(chat_id, format_error("Cüzdan kaydedilemedi."))
            return
        await self._send(chat_id, format_watch_result(address, added))

    async def _handle_unwatch_command(self, parts: list[str], user_id: str, chat_id: str) -> None:
        if len(parts) != 3 or not self.WALLET_ADDRESS_RE.fullmatch(parts[2]):
            await self._send(chat_id, format_error("Kullanım: /unwatch <ağ> <adres>", main_menu_keyboard()))
            return
        address = parts[2].lower()
        removed = self.wallet_store.remove_wallet(user_id, parts[1], address)
        await self._send(chat_id, format_remove_result(address, removed))

    async def _handle_history_command(self, parts: list[str], chat_id: str) -> None:
        if len(parts) != 3 or not self.WALLET_ADDRESS_RE.fullmatch(parts[2]):
            await self._send(chat_id, format_error("Kullanım: /history <ağ> <adres>", main_menu_keyboard()))
            return
        await self._show_history(chat_id, parts[1].lower(), parts[2].lower())

    async def _handle_callback_query(self, callback: dict[str, Any]) -> None:
        callback_id = callback.get("id")
        if not isinstance(callback_id, str):
            return
        message = callback.get("message")
        sender = callback.get("from")
        chat = message.get("chat") if isinstance(message, dict) else None
        is_private_self_chat = (
            isinstance(chat, dict)
            and isinstance(sender, dict)
            and chat.get("type") == "private"
            and str(chat.get("id")) == str(sender.get("id"))
        )
        # Acknowledge first so Telegram's loading indicator disappears before work/API calls.
        try:
            await self.notifier.answer_callback_query(
                callback_id,
                None if is_private_self_chat else "Cüzdan menüsünü özel sohbette kullan.",
                show_alert=not is_private_self_chat,
            )
        except TelegramError as exc:
            logger.warning("Menü düğmesi yanıtlanamadı: %s", exc)

        data = callback.get("data")
        if not isinstance(data, str) or not isinstance(message, dict) or not isinstance(sender, dict):
            return
        message_id = message.get("message_id")
        if not isinstance(chat, dict) or not isinstance(message_id, int):
            return
        chat_id = str(chat.get("id"))
        user_id = str(sender.get("id"))
        if chat.get("type") != "private" or chat_id != user_id:
            return

        try:
            await self._dispatch_callback(data, user_id, chat_id, message_id, sender)
        except (TelegramError, OSError, ValueError) as exc:
            logger.warning("Menü işlemi tamamlanamadı: %s", exc)
            await self._edit(chat_id, message_id, format_error("İşlem tamamlanamadı. /start ile menüyü yenile."))

    async def _dispatch_callback(
        self,
        data: str,
        user_id: str,
        chat_id: str,
        message_id: int,
        sender: dict[str, Any],
    ) -> None:
        wallets = self.wallet_store.for_user(user_id)
        if data == MENU_MAIN:
            await self._edit(chat_id, message_id, format_start(
                self.bot_name,
                str(sender.get("first_name") or "kullanıcı"),
                len(wallets),
            ))
        elif data == MENU_WALLETS:
            await self._edit(chat_id, message_id, format_wallet_list(wallets))
        elif data == MENU_ADD:
            await self._edit(chat_id, message_id, format_add_network())
        elif data == MENU_HISTORY:
            if not wallets:
                await self._edit(chat_id, message_id, format_wallet_list(wallets))
            else:
                await self._edit(
                    chat_id,
                    message_id,
                    format_history_selection(wallets),
                )
        elif data == MENU_STATUS:
            await self._edit(chat_id, message_id, format_status(wallets))
        elif data == MENU_HELP:
            await self._edit(chat_id, message_id, format_help())
        elif data == MENU_ABOUT:
            await self._edit(chat_id, message_id, format_about())
        elif data.startswith(WALLET_ADD_PREFIX):
            network = data[len(WALLET_ADD_PREFIX):]
            if network not in NETWORKS:
                await self._edit(chat_id, message_id, format_stale_menu())
                return
            self._pending_addresses[user_id] = PendingAddress(
                network,
                chat_id,
                message_id,
                time.monotonic() + self.PENDING_ADDRESS_TTL,
            )
            await self._edit(chat_id, message_id, format_add_prompt(network))
        elif data == WALLET_CANCEL:
            self._pending_addresses.pop(user_id, None)
            await self._edit(chat_id, message_id, format_start(
                self.bot_name,
                str(sender.get("first_name") or "kullanıcı"),
                len(wallets),
            ))
        elif data.startswith(WALLET_LIST_PREFIX):
            try:
                page = max(0, int(data[len(WALLET_LIST_PREFIX):]))
            except ValueError:
                await self._edit(chat_id, message_id, format_stale_menu())
                return
            max_page = max(0, (len(wallets) - 1) // self.PAGE_SIZE)
            await self._edit(chat_id, message_id, format_wallet_list(wallets, min(page, max_page), self.PAGE_SIZE))
        elif data.startswith(WALLET_HISTORY_REFRESH_PREFIX):
            address = data[len(WALLET_HISTORY_REFRESH_PREFIX):].lower()
            if not self._owns_wallet(wallets, address):
                await self._edit(chat_id, message_id, format_stale_menu())
                return
            await self._show_history(chat_id, "ethereum", address, edit_message_id=message_id)
        elif data.startswith(WALLET_HISTORY_PREFIX):
            address = data[len(WALLET_HISTORY_PREFIX):].lower()
            if not self._owns_wallet(wallets, address):
                await self._edit(chat_id, message_id, format_stale_menu())
                return
            await self._show_history(chat_id, "ethereum", address, edit_message_id=message_id)
        elif data.startswith(WALLET_REMOVE_CONFIRM_PREFIX):
            address = data[len(WALLET_REMOVE_CONFIRM_PREFIX):].lower()
            if not self._owns_wallet(wallets, address):
                await self._edit(chat_id, message_id, format_stale_menu())
                return
            removed = self.wallet_store.remove_wallet(user_id, "ethereum", address)
            remaining = self.wallet_store.for_user(user_id)
            await self._edit(
                chat_id,
                message_id,
                format_remove_result(address, removed),
            )
            if remaining and removed:
                logger.info("Kullanıcı kendi cüzdan izleme kaydını kaldırdı.")
        elif data.startswith(WALLET_REMOVE_CANCEL_PREFIX):
            address = data[len(WALLET_REMOVE_CANCEL_PREFIX):].lower()
            if not self._owns_wallet(wallets, address):
                await self._edit(chat_id, message_id, format_stale_menu())
                return
            await self._edit(chat_id, message_id, format_wallet_list(wallets, page_size=self.PAGE_SIZE))
        elif data.startswith(WALLET_REMOVE_PREFIX):
            address = data[len(WALLET_REMOVE_PREFIX):].lower()
            if not self._owns_wallet(wallets, address):
                await self._edit(chat_id, message_id, format_stale_menu())
                return
            await self._edit(chat_id, message_id, format_remove_confirmation(address))
        else:
            await self._edit(chat_id, message_id, format_stale_menu())

    @staticmethod
    def _owns_wallet(wallets: list[WalletSubscription], address: str) -> bool:
        return any(item.network == "ethereum" and item.wallet_address.lower() == address.lower() for item in wallets)

    async def _show_history(
        self,
        chat_id: str,
        network: str,
        address: str,
        edit_message_id: int | None = None,
    ) -> None:
        keyboard = history_result_keyboard(address)
        try:
            transfers = await self.monitor.get_recent_transfers(network, address, 10)
        except Exception as exc:
            logger.warning("Cüzdan geçmişi alınamadı: %s", exc)
            message = format_error(f"Geçmiş alınamadı: {exc}", keyboard)
            await self._show_formatted(chat_id, message, edit_message_id)
            return
        if not transfers:
            await self._show_formatted(
                chat_id,
                format_history_empty(address, keyboard),
                edit_message_id,
            )
            return
        try:
            prices = await self.price_client.get_usd_prices(
                [transfer.token_address for transfer in transfers]
            )
        except Exception:
            logger.exception("Transfer geçmişi için USD fiyatları alınamadı.")
            prices = {}
        messages = format_history(
            transfers,
            prices,
            address,
            keyboard,
        )
        await self._show_formatted(chat_id, messages[0], edit_message_id)
        for message in messages[1:]:
            await self._send(chat_id, message)

    async def _show_formatted(
        self,
        chat_id: str,
        message: FormattedMessage,
        edit_message_id: int | None,
    ) -> None:
        if edit_message_id is None:
            await self._send(chat_id, message)
        else:
            await self._edit(chat_id, edit_message_id, message)

    async def _send(self, chat_id: str, message: FormattedMessage) -> None:
        try:
            await self.notifier.send_formatted(message, chat_id)
        except TelegramError:
            logger.exception("Telegram mesajı gönderilemedi.")

    async def _edit(self, chat_id: str, message_id: int, message: FormattedMessage) -> None:
        try:
            await self.notifier.edit_formatted(chat_id, message_id, message)
        except TelegramError:
            logger.exception("Telegram menü mesajı düzenlenemedi.")

    async def close(self) -> None:
        if self.session is not None and not self.session.closed:
            await self.session.close()
