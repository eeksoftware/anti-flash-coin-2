import asyncio
import logging
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from analyzer.token_analyzer import TokenAnalysis
from audit_log import CLASSIFICATION_LABELS
from blockchain.token_price import DexScreenerPriceClient, ETHEREUM_WETH_ADDRESS
from blockchain.etherscan import EtherscanError, EtherscanRateLimitError, EtherscanTimeoutError
from models.transaction import TokenTransfer
from networks import get_network_config
from config import settings
from services.check_input import InvalidCheckInput, parse_check_args
from services.check_limits import CheckRateLimiter
from services.check_service import CheckService
from providers.tron import TronGridError, TronGridRateLimitError, TronGridTimeoutError
from monitor.state_db import (
    SettingsInputError,
    UserStateStore,
    parse_alias,
    parse_feedback_callback,
    parse_min_amount,
    parse_quiet_hours,
)
from monitor.wallet_monitor import WalletMonitorManager
from reporting.access import (
    ExportTooLarge,
    InvalidRange,
    ReportError,
    ReportReader,
    UnauthorizedWallet,
    export_filename,
    parse_export_args,
    write_csv,
)
from monitor.wallet_store import WalletStateStore, WalletSubscription
from notifications.telegram_formatter import (
    FormattedMessage,
    format_about,
    format_check_error,
    format_check_loading,
    format_check_prompt,
    format_check_usage,
    format_add_expired,
    format_add_invalid,
    format_add_network,
    format_add_prompt,
    format_api_status,
    format_error,
    format_export_notice,
    format_export_prompt,
    format_group_start,
    format_help,
    format_history,
    format_history_empty,
    format_history_loading,
    format_history_selection,
    format_remove_confirmation,
    format_remove_result,
    format_stale_menu,
    format_setting_prompt,
    format_start,
    format_status,
    format_token_check,
    format_tx_check,
    format_approval_alert,
    format_fee_alert,
    format_transfer_alert,
    format_wallet_list,
    format_wallet_settings,
    format_health_report,
    format_stats_report,
    format_watch_result,
    escape,
)
from notifications.admin import admin_command_allowed
from notifications.telegram_keyboards import (
    MENU_ABOUT,
    MENU_ADD,
    MENU_CHECK,
    MENU_HELP,
    MENU_HISTORY,
    MENU_MAIN,
    MENU_STATUS,
    MENU_WALLETS,
    NETWORKS,
    WALLET_ADD_PREFIX,
    WALLET_CANCEL,
    WALLET_HISTORY_PREFIX,
    WALLET_ALIAS_PREFIX,
    WALLET_HISTORY_REFRESH_PREFIX,
    WALLET_LIST_PREFIX,
    WALLET_MIN_PREFIX,
    WALLET_QUIET_CLEAR_PREFIX,
    WALLET_QUIET_PREFIX,
    WALLET_REMOVE_CANCEL_PREFIX,
    WALLET_REMOVE_CONFIRM_PREFIX,
    WALLET_REMOVE_PREFIX,
    WALLET_SETTINGS_PREFIX,
    WALLET_SUSPICIOUS_PREFIX,
    WALLET_FEE_PREFIX,
    WALLET_WEEKLY_PREFIX,
    EXPORT_24H,
    EXPORT_7D,
    EXPORT_30D,
    EXPORT_CUSTOM,
    cancel_keyboard,
    history_result_keyboard,
    main_menu_keyboard,
    mark_selected_feedback,
    with_feedback_status,
    without_feedback_status,
)


logger = logging.getLogger(__name__)


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _entities_within(entities: Any, text: str) -> list[dict[str, Any]] | None:
    """Keep message entities that still fall inside the original alert text."""
    if not isinstance(entities, list):
        return None
    limit = _utf16_length(text)
    kept: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict) or not isinstance(entity.get("type"), str):
            continue
        offset = entity.get("offset")
        length = entity.get("length")
        if (
            isinstance(offset, int)
            and isinstance(length, int)
            and offset >= 0
            and length > 0
            and offset + length <= limit
        ):
            kept.append(entity)
    return kept or None


def format_activity_notice(sender: dict[str, Any], body: str) -> str:
    user_id = escape(sender.get("id", ""))
    name = escape(sender.get("first_name") or sender.get("username") or "kullanıcı")
    username = sender.get("username")
    handle = f" (@{escape(username)})" if isinstance(username, str) and username else ""
    return f"👤 <b>{name}</b>{handle} · <code>{user_id}</code>\n{body}"


class TelegramError(Exception):
    """Telegram API ile iletişim hatası."""


@dataclass(frozen=True)
class PendingAddress:
    network: str
    chat_id: str
    message_id: int
    expires_at: float


@dataclass(frozen=True)
class PendingCheck:
    chat_id: str
    message_id: int
    expires_at: float


@dataclass(frozen=True)
class PendingSetting:
    network: str
    address: str
    field: str
    chat_id: str
    message_id: int
    expires_at: float


@dataclass(frozen=True)
class PendingExport:
    chat_id: str
    message_id: int
    expires_at: float


async def _ethereum_address_names(check_service, result) -> dict[str, str]:
    """Name tags for an Ethereum check. Other networks and API failures stay unlabeled."""
    if check_service is None or getattr(result, "network", "") != "ethereum":
        return {}
    provider = getattr(check_service, "providers", {}).get("ethereum")
    client = getattr(provider, "client", None)
    lookup = getattr(client, "get_address_tags", None)
    if lookup is None:
        return {}
    addresses = []
    transaction = getattr(result, "transaction", None)
    if transaction is not None:
        addresses.extend([transaction.from_address, transaction.to_address])
    for item in getattr(result, "transfers", ()) or ():
        transfer = getattr(item, "transfer", None)
        if transfer is None:
            continue
        addresses.extend([transfer.sender, transfer.receiver, transfer.token_address])
    try:
        names = await lookup([address for address in addresses if address])
    except Exception:
        logger.warning("Adres adı alınamadı.")
        return {}
    return names if isinstance(names, dict) else {}


class TelegramNotifier:
    BASE_URL = "https://api.telegram.org"

    def __init__(self, bot_token: str, chat_id: str = "", health_state=None, chat_store=None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.health_state = health_state
        self.chat_store = chat_store
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
        if self.health_state is not None:
            self.health_state.last_telegram_success_at = datetime.now(timezone.utc)
        result = response.get("result")
        sent = result if isinstance(result, dict) else {}
        self._remember_out(payload["chat_id"], sent, message.text)
        return sent

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
        response = await self.call_api("sendMessage", payload)
        result = response.get("result") if isinstance(response, dict) else None
        self._remember_out(payload["chat_id"], result if isinstance(result, dict) else {}, text)
        if self.health_state is not None:
            self.health_state.last_telegram_success_at = datetime.now(timezone.utc)

    async def send_document(self, chat_id: str | int, path, filename: str, caption: str = "") -> None:
        session = await self._ensure_session()
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if caption:
            form.add_field("caption", caption)
        handle = path.open("rb")
        form.add_field("document", handle, filename=filename, content_type="text/csv")
        try:
            async with session.post(
                f"{self.BASE_URL}/bot{self.bot_token}/sendDocument",
                data=form,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except (aiohttp.ClientError, ValueError) as exc:
                    raise TelegramError("Dosya gönderilemedi.") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise TelegramError("Dosya gönderilemedi.") from exc
        finally:
            handle.close()
        if not isinstance(data, dict) or not data.get("ok"):
            raise TelegramError("Dosya gönderilemedi.")
        if self.health_state is not None:
            self.health_state.last_telegram_success_at = datetime.now(timezone.utc)

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
        self._revise_out(chat_id, message_id, message.text)
        if self.health_state is not None:
            self.health_state.last_telegram_success_at = datetime.now(timezone.utc)

    async def edit_text(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        entities: list[dict[str, Any]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        if entities:
            payload["entities"] = entities
        await self.call_api("editMessageText", payload, ignore_not_modified=True)

    async def edit_reply_markup(
        self,
        chat_id: str | int,
        message_id: int,
        reply_markup: dict[str, Any],
    ) -> None:
        await self.call_api(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup},
            ignore_not_modified=True,
        )

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
        *,
        alias: str | None = None,
        feedback_token: str | None = None,
        risk=None,
        poisoning=None,
        address_names: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self.send_formatted(
            format_transfer_alert(
                transfer, analysis, alias=alias, feedback_token=feedback_token,
                risk=risk, poisoning=poisoning, address_names=address_names,
            ),
            chat_id,
        )

    async def send_fee_alert(
        self,
        transfer: TokenTransfer,
        usd,
        chat_id: str | int | None = None,
        *,
        alias: str | None = None,
        address_names: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self.send_formatted(
            format_fee_alert(transfer, usd, alias=alias, address_names=address_names),
            chat_id,
        )

    async def send_approval_alert(
        self,
        event,
        assessment,
        risk,
        chat_id: str | int | None = None,
        *,
        alias: str | None = None,
        token_symbol: str | None = None,
    ) -> dict[str, Any]:
        return await self.send_formatted(
            format_approval_alert(event, assessment, risk, alias=alias, token_symbol=token_symbol),
            chat_id,
        )

    async def send_api_status_alert(
        self,
        status: str,
        chat_id: str | int | None = None,
    ) -> None:
        await self.send_formatted(format_api_status(status), chat_id)

    def _remember_out(self, chat_id, result: dict, text: str) -> None:
        if self.chat_store is None or not isinstance(result, dict):
            return
        message_id = result.get("message_id")
        if not isinstance(message_id, int):
            return
        sender = result.get("from") if isinstance(result.get("from"), dict) else {}
        name = str(sender.get("first_name") or "Bot")
        try:
            self.chat_store.add(
                chat_id=str(chat_id),
                message_id=message_id,
                direction="out",
                user_id=str(sender.get("id") or ""),
                sender_name=name,
                username=str(sender.get("username") or ""),
                body=text,
                sent_at=_message_time(result.get("date")),
            )
        except Exception:
            logger.warning("Sohbet kaydı yazılamadı.")

    def _revise_out(self, chat_id, message_id: int, text: str) -> None:
        if self.chat_store is None or not isinstance(message_id, int):
            return
        try:
            self.chat_store.revise(str(chat_id), message_id, text)
        except Exception:
            logger.warning("Sohbet kaydı güncellenemedi.")

    async def close(self) -> None:
        if self.session is not None and not self.session.closed:
            await self.session.close()


def _message_time(value) -> str:
    if isinstance(value, int):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


class TelegramBotController:
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
        check_service: CheckService | None = None,
        *,
        admin_ids=(),
        health_reporter=None,
        stats_reporter=None,
        operations_store=None,
        activity_log_chat_id: str | None = None,
        user_state: UserStateStore | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.monitor = monitor
        self.notifier = notifier
        self.wallet_store = wallet_store
        self.price_client = price_client
        self.check_service = check_service
        self.admin_ids = frozenset(str(value) for value in admin_ids)
        self.health_reporter = health_reporter
        self.stats_reporter = stats_reporter
        self.operations_store = operations_store
        self.activity_log_chat_id = (
            settings.activity_log_chat_id if activity_log_chat_id is None else activity_log_chat_id
        )
        self.session: aiohttp.ClientSession | None = None
        self._offset: int | None = None
        self.bot_name = "Anti-Flash Token Bot"
        self.bot_username: str | None = None
        self.user_state = user_state
        self._pending_addresses: dict[str, PendingAddress] = {}
        self._pending_checks: dict[str, PendingCheck] = {}
        self._pending_settings: dict[str, PendingSetting] = {}
        self._pending_exports: dict[str, PendingExport] = {}
        self._check_rate_limiter = CheckRateLimiter()

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
            {"command": "check", "description": "Token ya da işlem kontrol et"},
            {"command": "status", "description": "İzleme durumunu göster"},
            {"command": "help", "description": "Yardım menüsünü aç"},
            {"command": "export", "description": "Transferleri CSV olarak indir"},
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
        self._remember_incoming(message, chat_id, sender, text)
        chat_type = chat.get("type")
        parts = text.strip().split()
        if not parts:
            return
        command = parts[0].split("@", 1)[0].lower()

        if command in {"/health", "/stats"}:
            if not admin_command_allowed(message, self.admin_ids):
                return
            try:
                if command == "/health" and self.health_reporter is not None:
                    await self._send(chat_id, format_health_report(self.health_reporter()))
                elif command == "/stats" and self.stats_reporter is not None:
                    await self._send(chat_id, format_stats_report(self.stats_reporter()))
            except Exception:
                logger.exception("Admin raporu oluşturulamadı.")
            return

        if chat_type != "private" or chat_id != user_id:
            if command == "/start":
                await self._send(chat_id, format_group_start(self.bot_username))
            else:
                logger.info("Özel sohbet dışından gelen cüzdan komutu yok sayıldı.")
            return

        if command == "/start":
            self._pending_addresses.pop(user_id, None)
            self._pending_checks.pop(user_id, None)
            self._pending_settings.pop(user_id, None)
            self._pending_exports.pop(user_id, None)
            await self._send(
                chat_id,
                format_start(
                    self.bot_name,
                    str(sender.get("first_name") or "kullanıcı"),
                    len(self.wallet_store.for_user(user_id)),
                ),
            )
            await self._report_activity(sender, "▶️ Botu açtı", source_chat_id=chat_id)
            return
        if command == "/help":
            await self._send(chat_id, format_help())
            return

        pending_check = self._pending_checks.get(user_id)
        if pending_check is not None and not command.startswith("/"):
            if time.monotonic() >= pending_check.expires_at:
                self._pending_checks.pop(user_id, None)
                await self._edit(pending_check.chat_id, pending_check.message_id, format_check_error("expired"))
                return
            self._pending_checks.pop(user_id, None)
            await self._handle_check([text.strip()], user_id, chat_id, pending_check.message_id, sender)
            return
        if pending_check is not None and time.monotonic() >= pending_check.expires_at:
            self._pending_checks.pop(user_id, None)
            await self._edit(pending_check.chat_id, pending_check.message_id, format_check_error("expired"))

        pending_export = self._pending_exports.get(user_id)
        if pending_export is not None and not command.startswith("/"):
            self._pending_exports.pop(user_id, None)
            if time.monotonic() >= pending_export.expires_at:
                await self._edit(pending_export.chat_id, pending_export.message_id, format_add_expired())
                return
            await self._run_export(user_id, chat_id, text.strip().split(), pending_export.message_id)
            return

        pending_setting = self._pending_settings.get(user_id)
        if pending_setting is not None and not command.startswith("/"):
            await self._accept_pending_setting(user_id, chat_id, text.strip(), pending_setting)
            return
        if pending_setting is not None and time.monotonic() >= pending_setting.expires_at:
            self._pending_settings.pop(user_id, None)
            await self._edit(pending_setting.chat_id, pending_setting.message_id, format_add_expired())

        pending = self._pending_addresses.get(user_id)
        if pending is not None and not command.startswith("/"):
            await self._accept_pending_address(user_id, chat_id, text.strip(), pending, sender)
            return
        if pending is not None and time.monotonic() >= pending.expires_at:
            self._pending_addresses.pop(user_id, None)
            await self._edit(pending.chat_id, pending.message_id, format_add_expired())

        if command == "/status":
            await self._send(chat_id, format_status(self.wallet_store.for_user(user_id)))
        elif command == "/wallets":
            wallets = self.wallet_store.for_user(user_id)
            await self._send(chat_id, self._wallet_list(user_id, wallets))
        elif command == "/watch":
            await self._handle_watch_command(parts, user_id, chat_id, sender)
        elif command == "/unwatch":
            await self._handle_unwatch_command(parts, user_id, chat_id, sender)
        elif command == "/history":
            await self._handle_history_command(parts, chat_id, sender)
        elif command == "/check":
            await self._handle_check(parts[1:], user_id, chat_id, sender=sender)
        elif command == "/export":
            self._pending_exports.pop(user_id, None)
            if len(parts) == 1:
                await self._send(chat_id, format_export_prompt())
            else:
                await self._run_export(user_id, chat_id, parts[1:])
        else:
            logger.info("Bilinmeyen Telegram komutu yok sayıldı.")

    def _remember_incoming(self, message: dict, chat_id: str, sender: dict, text: str) -> None:
        store = getattr(self.notifier, "chat_store", None)
        message_id = message.get("message_id")
        if store is None or not isinstance(message_id, int):
            return
        first = str(sender.get("first_name") or "")
        last = str(sender.get("last_name") or "")
        name = " ".join(part for part in (first, last) if part)
        try:
            store.add(
                chat_id=chat_id,
                message_id=message_id,
                direction="in",
                user_id=str(sender.get("id") or ""),
                sender_name=name,
                username=str(sender.get("username") or ""),
                body=text,
                sent_at=_message_time(message.get("date")),
            )
        except Exception:
            logger.warning("Sohbet kaydı yazılamadı.")

    async def _accept_pending_address(
        self,
        user_id: str,
        chat_id: str,
        address: str,
        pending: PendingAddress,
        sender: dict[str, Any] | None = None,
    ) -> None:
        if time.monotonic() >= pending.expires_at:
            self._pending_addresses.pop(user_id, None)
            await self._edit(pending.chat_id, pending.message_id, format_add_expired())
            return
        address_config = get_network_config(pending.network)
        if chat_id != pending.chat_id or not address_config.is_valid_address(address):
            await self._edit(pending.chat_id, pending.message_id, format_add_invalid(pending.network))
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
            format_watch_result(address, added, pending.network),
        )
        await self._report_activity(
            sender,
            self._wallet_activity(
                "eklendi" if added else "zaten izleniyor",
                pending.network,
                address_config.normalize_address(address),
            ),
            source_chat_id=chat_id,
        )

    async def _run_export(self, user_id: str, chat_id: str, parts: list[str], message_id: int | None = None) -> None:
        async def tell(text: str) -> None:
            message = format_export_notice(text)
            try:
                if message_id is None:
                    await self._send(chat_id, message)
                else:
                    await self._edit(chat_id, message_id, message)
            except (TelegramError, OSError):
                logger.warning("CSV sonucu iletilemedi.")

        if self.user_state is None:
            await tell("Raporlar şu an kullanılamıyor. İzleme sürüyor.")
            return
        try:
            request = parse_export_args(parts)
        except InvalidRange as exc:
            await tell(str(exc))
            return
        allowed = [(item.network, item.wallet_address) for item in self.wallet_store.for_user(user_id)]
        operations = getattr(self.operations_store, "path", None)
        reader = ReportReader(self.user_state.path, operations)
        path: Path | None = None
        try:
            rows = reader.iter_rows(
                user_id=user_id,
                allowed=allowed,
                period=request.period,
                network=request.network,
                classification=request.classification,
                detection=request.detection,
                wallet=request.wallet,
            )
            path, count = write_csv(rows, Path(tempfile.gettempdir()))
            if count == 0:
                await tell("Bu aralıkta kayıt yok.")
                return
            caption = f"CSV · {request.period.start:%Y-%m-%d} – {request.period.end:%Y-%m-%d} UTC"
            await self.notifier.send_document(chat_id, path, export_filename(request.period), caption)
            if message_id is not None:
                await self._edit(chat_id, message_id, format_export_notice("Dosya gönderildi."))
        except UnauthorizedWallet as exc:
            await tell(str(exc))
        except ExportTooLarge as exc:
            await tell(str(exc))
        except (ReportError, TelegramError, OSError):
            logger.warning("CSV dışa aktarma başarısız.")
            await tell("Rapor oluşturulamadı. İzleme sürüyor.")
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    async def _handle_watch_command(
        self,
        parts: list[str],
        user_id: str,
        chat_id: str,
        sender: dict[str, Any] | None = None,
    ) -> None:
        if len(parts) != 3:
            await self._send(chat_id, format_error("Kullanım: /watch <ağ> <adres>", main_menu_keyboard()))
            return
        network = parts[1].lower()
        address = parts[2]
        if network not in NETWORKS:
            await self._send(chat_id, format_error("Bu ağ etkin değil.", main_menu_keyboard()))
            return
        config = get_network_config(network)
        if not config.is_valid_address(address):
            await self._send(chat_id, format_error("Cüzdan adresi geçersiz.", main_menu_keyboard()))
            return
        address = config.normalize_address(address)
        try:
            added = self.wallet_store.add_wallet(user_id, network, address)
        except (OSError, ValueError) as exc:
            logger.error("İstenen cüzdan kaydedilemedi: %s", exc)
            await self._send(chat_id, format_error("Cüzdan kaydedilemedi."))
            return
        await self._send(chat_id, format_watch_result(address, added, network))
        await self._report_activity(
            sender,
            self._wallet_activity("eklendi" if added else "zaten izleniyor", network, address),
            source_chat_id=chat_id,
        )

    async def _handle_unwatch_command(
        self,
        parts: list[str],
        user_id: str,
        chat_id: str,
        sender: dict[str, Any] | None = None,
    ) -> None:
        if len(parts) != 3 or parts[1].lower() not in NETWORKS:
            await self._send(chat_id, format_error("Kullanım: /unwatch <ağ> <adres>", main_menu_keyboard()))
            return
        network = parts[1].lower()
        config = get_network_config(network)
        if not config.is_valid_address(parts[2]):
            await self._send(chat_id, format_error("Cüzdan adresi geçersiz.", main_menu_keyboard()))
            return
        address = config.normalize_address(parts[2])
        removed = self.wallet_store.remove_wallet(user_id, network, address)
        await self._send(chat_id, format_remove_result(address, removed, network))
        await self._report_activity(
            sender,
            self._wallet_activity("kaldırıldı" if removed else "kayıtlı değil", network, address),
            source_chat_id=chat_id,
        )

    async def _handle_history_command(
        self,
        parts: list[str],
        chat_id: str,
        sender: dict[str, Any] | None = None,
    ) -> None:
        if len(parts) != 3 or parts[1].lower() not in NETWORKS:
            await self._send(chat_id, format_error("Kullanım: /history <ağ> <adres>", main_menu_keyboard()))
            return
        network = parts[1].lower()
        config = get_network_config(network)
        if not config.is_valid_address(parts[2]):
            await self._send(chat_id, format_error("Cüzdan adresi geçersiz.", main_menu_keyboard()))
            return
        await self._show_history(chat_id, network, config.normalize_address(parts[2]), sender=sender)

    async def _handle_check(
        self,
        args: list[str],
        user_id: str,
        chat_id: str,
        edit_message_id: int | None = None,
        sender: dict[str, Any] | None = None,
    ) -> None:
        if self.operations_store is not None:
            self.operations_store.increment("check_usage")
        try:
            request = parse_check_args(args)
        except InvalidCheckInput:
            await self._show_formatted(chat_id, format_check_usage(), edit_message_id)
            return
        except ValueError:
            await self._show_formatted(chat_id, format_check_usage(), edit_message_id)
            return
        if self.check_service is None:
            await self._show_formatted(chat_id, format_check_error("provider"), edit_message_id)
            return
        if not self._check_rate_limiter.allow(user_id):
            await self._show_formatted(chat_id, format_check_error("too_fast"), edit_message_id)
            return
        progress_message_id = await self._show_progress(
            chat_id, format_check_loading(), edit_message_id
        )
        try:
            if request.kind == "token":
                result = await self.check_service.check_token(request.network, request.value)
                message = format_token_check(result)
            else:
                result = await self.check_service.check_transaction(request.network, request.value)
                eth_usd_price = None
                if result.network == "ethereum" and result.transaction is not None and result.transaction.native_value > 0:
                    try:
                        prices = await self.price_client.get_usd_prices([ETHEREUM_WETH_ADDRESS])
                        eth_usd_price = prices.get(ETHEREUM_WETH_ADDRESS)
                    except Exception:
                        logger.warning("Native ETH için USD fiyatı alınamadı.")
                address_names = await _ethereum_address_names(self.check_service, result)
                message = format_tx_check(result, eth_usd_price=eth_usd_price, address_names=address_names)
        except (EtherscanRateLimitError, TronGridRateLimitError):
            if self.operations_store is not None: self.operations_store.increment("errors")
            message = format_check_error("rate_limit")
            outcome = "Tamamlanamadı"
        except (EtherscanTimeoutError, EtherscanError, TronGridTimeoutError, TronGridError):
            if self.operations_store is not None: self.operations_store.increment("errors")
            logger.warning("Manuel kontrol sağlayıcı hatası: %s", "API_ERROR")
            message = format_check_error("provider")
            outcome = "Tamamlanamadı"
        except Exception as exc:
            if self.operations_store is not None: self.operations_store.increment("errors")
            logger.warning("Manuel kontrol tamamlanamadı: %s", type(exc).__name__)
            message = format_check_error("provider")
            outcome = "Tamamlanamadı"
        else:
            transaction = getattr(result, "transaction", None)
            native_only = (
                request.kind != "token"
                and transaction is not None
                and transaction.native_value > 0
                and not result.transfers
            )
            if native_only and transaction.status == "success":
                outcome = "TRX transferi" if result.network == "tron" else "ETH transferi"
            else:
                classification = (
                    result.analysis.classification if result.analysis is not None else "ERROR"
                ) if request.kind == "token" else result.classification
                outcome = CLASSIFICATION_LABELS.get(classification, classification)
        await self._show_formatted(chat_id, message, progress_message_id)
        kind = "Token" if request.kind == "token" else "İşlem"
        await self._report_activity(
            sender,
            f"🔎 {kind} kontrolü · {escape(request.network)}\n<code>{escape(request.value)}</code>\n{escape(outcome)}",
            source_chat_id=chat_id,
        )

    async def _handle_callback_query(self, callback: dict[str, Any]) -> None:
        callback_id = callback.get("id")
        if not isinstance(callback_id, str):
            return
        message = callback.get("message")
        sender = callback.get("from")
        data = callback.get("data")
        if isinstance(data, str) and data.startswith("fb:"):
            await self._handle_feedback_callback(callback_id, data, message, sender)
            return
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
        export_args = {EXPORT_24H: "24h", EXPORT_7D: "7d", EXPORT_30D: "30d"}
        if data in export_args:
            await self._run_export(user_id, chat_id, [export_args[data]], message_id)
            return
        if data == EXPORT_CUSTOM:
            self._pending_exports[user_id] = PendingExport(
                chat_id, message_id, time.monotonic() + self.PENDING_ADDRESS_TTL,
            )
            await self._edit(chat_id, message_id, format_export_prompt())
            return
        if data == MENU_MAIN:
            await self._edit(chat_id, message_id, format_start(
                self.bot_name,
                str(sender.get("first_name") or "kullanıcı"),
                len(wallets),
            ))
        elif data == MENU_WALLETS:
            await self._edit(chat_id, message_id, self._wallet_list(user_id, wallets))
        elif data == MENU_ADD:
            await self._edit(chat_id, message_id, format_add_network())
        elif data == MENU_CHECK:
            self._pending_addresses.pop(user_id, None)
            self._pending_checks[user_id] = PendingCheck(
                chat_id, message_id, time.monotonic() + self.PENDING_ADDRESS_TTL
            )
            await self._edit(chat_id, message_id, format_check_prompt())
        elif data == MENU_HISTORY:
            if not wallets:
                await self._edit(chat_id, message_id, self._wallet_list(user_id, wallets))
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
            self._pending_checks.pop(user_id, None)
            self._pending_settings.pop(user_id, None)
            self._pending_exports.pop(user_id, None)
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
            await self._edit(chat_id, message_id, self._wallet_list(user_id, wallets, min(page, max_page), self.PAGE_SIZE))
        elif data.startswith(WALLET_HISTORY_REFRESH_PREFIX):
            decoded = self._decode_wallet_callback(data[len(WALLET_HISTORY_REFRESH_PREFIX):], wallets)
            if decoded is None:
                await self._edit(chat_id, message_id, format_stale_menu())
                return
            network, address = decoded
            await self._show_history(chat_id, network, address, edit_message_id=message_id, sender=sender)
        elif data.startswith(WALLET_HISTORY_PREFIX):
            decoded = self._decode_wallet_callback(data[len(WALLET_HISTORY_PREFIX):], wallets)
            if decoded is None:
                await self._edit(chat_id, message_id, format_stale_menu())
                return
            network, address = decoded
            await self._show_history(chat_id, network, address, edit_message_id=message_id, sender=sender)
        elif data.startswith(WALLET_REMOVE_CONFIRM_PREFIX):
            decoded = self._decode_wallet_callback(data[len(WALLET_REMOVE_CONFIRM_PREFIX):], wallets)
            if decoded is None:
                await self._edit(chat_id, message_id, format_stale_menu())
                return
            network, address = decoded
            removed = self.wallet_store.remove_wallet(user_id, network, address)
            remaining = self.wallet_store.for_user(user_id)
            await self._edit(
                chat_id,
                message_id,
                format_remove_result(address, removed, network),
            )
            if remaining and removed:
                logger.info("Kullanıcı kendi cüzdan izleme kaydını kaldırdı.")
            await self._report_activity(
                sender,
                self._wallet_activity("kaldırıldı" if removed else "kayıtlı değil", network, address),
                source_chat_id=chat_id,
            )
        elif data.startswith(WALLET_REMOVE_CANCEL_PREFIX):
            decoded = self._decode_wallet_callback(data[len(WALLET_REMOVE_CANCEL_PREFIX):], wallets)
            if decoded is None:
                await self._edit(chat_id, message_id, format_stale_menu())
                return
            await self._edit(chat_id, message_id, self._wallet_list(user_id, wallets, page_size=self.PAGE_SIZE))
        elif data.startswith(WALLET_REMOVE_PREFIX):
            decoded = self._decode_wallet_callback(data[len(WALLET_REMOVE_PREFIX):], wallets)
            if decoded is None:
                await self._edit(chat_id, message_id, format_stale_menu())
                return
            network, address = decoded
            await self._edit(chat_id, message_id, format_remove_confirmation(address, network))
        elif self._is_settings_callback(data):
            await self._handle_settings_callback(data, user_id, chat_id, message_id, wallets)
        else:
            await self._edit(chat_id, message_id, format_stale_menu())

    @staticmethod
    def _decode_wallet_callback(value: str, wallets: list[WalletSubscription]):
        if ":" in value:
            network, address = value.split(":", 1)
        else:
            network, address = "ethereum", value
        try:
            config = get_network_config(network)
            normalized = config.normalize_address(address)
        except ValueError:
            return None
        if not config.is_valid_address(normalized):
            return None
        if any(item.network == network and item.wallet_address == normalized for item in wallets):
            return network, normalized
        return None

    async def _show_history(
        self,
        chat_id: str,
        network: str,
        address: str,
        edit_message_id: int | None = None,
        sender: dict[str, Any] | None = None,
    ) -> None:
        config = get_network_config(network)
        address = config.normalize_address(address)
        await self._report_activity(
            sender,
            f"📜 Geçmiş · {escape(network)}\n<code>{escape(address)}</code>",
            source_chat_id=chat_id,
        )
        keyboard = history_result_keyboard(address, network)
        progress_message_id = await self._show_progress(
            chat_id, format_history_loading(address), edit_message_id
        )
        try:
            transfers = await self.monitor.get_recent_transfers(network, address, 10)
        except Exception as exc:
            logger.warning("Cüzdan geçmişi alınamadı: %s", exc)
            message = format_error(f"Geçmiş alınamadı: {exc}", keyboard)
            await self._show_formatted(chat_id, message, progress_message_id)
            return
        if not transfers:
            await self._show_formatted(
                chat_id,
                format_history_empty(address, keyboard, network),
                progress_message_id,
            )
            return
        prices = {}
        if network == "ethereum":
            try:
                prices = await self.price_client.get_usd_prices(
                    [transfer.token_address for transfer in transfers]
                )
            except Exception:
                logger.exception("Transfer geçmişi için USD fiyatları alınamadı.")
        messages = format_history(
            transfers,
            prices,
            address,
            keyboard,
        )
        await self._show_formatted(chat_id, messages[0], progress_message_id)
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

    async def _show_progress(
        self,
        chat_id: str,
        message: FormattedMessage,
        edit_message_id: int | None,
    ) -> int | None:
        if edit_message_id is not None:
            await self._edit(chat_id, edit_message_id, message)
            return edit_message_id
        return await self._send(chat_id, message)

    def _aliases(self, user_id: str, wallets: list[WalletSubscription]) -> dict[tuple[str, str], str]:
        if self.user_state is None:
            return {}
        found: dict[tuple[str, str], str] = {}
        for wallet in wallets:
            try:
                prefs = self.user_state.settings(user_id, wallet.network, wallet.wallet_address)
            except Exception:
                logger.warning("Cüzdan ayarları okunamadı; liste varsayılan adla gösteriliyor.")
                continue
            if prefs.alias:
                found[(wallet.network, wallet.wallet_address)] = prefs.alias
        return found

    def _wallet_list(self, user_id: str, wallets: list[WalletSubscription], page: int = 0, page_size: int = 8):
        return format_wallet_list(wallets, page, page_size, self._aliases(user_id, wallets))

    def _owns_wallet(self, user_id: str, network: str, address: str) -> bool:
        return any(
            item.network == network and item.wallet_address == address
            for item in self.wallet_store.for_user(user_id)
        )

    @staticmethod
    def _is_settings_callback(data: str) -> bool:
        return data.startswith((
            WALLET_SETTINGS_PREFIX,
            WALLET_ALIAS_PREFIX,
            WALLET_MIN_PREFIX,
            WALLET_SUSPICIOUS_PREFIX,
            WALLET_QUIET_PREFIX,
            WALLET_QUIET_CLEAR_PREFIX,
            WALLET_WEEKLY_PREFIX,
            WALLET_FEE_PREFIX,
        ))

    async def _handle_settings_callback(self, data, user_id, chat_id, message_id, wallets) -> None:
        if data.startswith(WALLET_SETTINGS_PREFIX):
            prefix = WALLET_SETTINGS_PREFIX
            field = "open"
        elif data.startswith(WALLET_ALIAS_PREFIX):
            prefix, field = WALLET_ALIAS_PREFIX, "alias"
        elif data.startswith(WALLET_MIN_PREFIX):
            prefix, field = WALLET_MIN_PREFIX, "amount"
        elif data.startswith(WALLET_SUSPICIOUS_PREFIX):
            prefix, field = WALLET_SUSPICIOUS_PREFIX, "suspicious"
        elif data.startswith(WALLET_QUIET_CLEAR_PREFIX):
            prefix, field = WALLET_QUIET_CLEAR_PREFIX, "quiet_clear"
        elif data.startswith(WALLET_WEEKLY_PREFIX):
            prefix, field = WALLET_WEEKLY_PREFIX, "weekly"
        elif data.startswith(WALLET_FEE_PREFIX):
            prefix, field = WALLET_FEE_PREFIX, "fee"
        elif data.startswith(WALLET_QUIET_PREFIX):
            prefix, field = WALLET_QUIET_PREFIX, "quiet"
        else:
            await self._edit(chat_id, message_id, format_stale_menu())
            return
        decoded = self._decode_wallet_callback(data[len(prefix):], wallets)
        if decoded is None or self.user_state is None:
            message = format_stale_menu() if decoded is None else format_error(
                "Ayarlar şu an kullanılamıyor.", main_menu_keyboard(),
            )
            await self._edit(chat_id, message_id, message)
            return
        network, address = decoded
        try:
            prefs = self.user_state.settings(user_id, network, address)
        except Exception:
            logger.warning("Cüzdan ayarları okunamadı.")
            await self._edit(chat_id, message_id, format_error(
                "Ayarlar okunamadı. Mevcut izleme sürüyor.", main_menu_keyboard(),
            ))
            return
        if field == "open":
            await self._edit(chat_id, message_id, format_wallet_settings(address, network, prefs))
            return
        if field in {"alias", "amount", "quiet"}:
            self._pending_addresses.pop(user_id, None)
            self._pending_checks.pop(user_id, None)
            self._pending_settings[user_id] = PendingSetting(
                network, address, field, chat_id, message_id, time.monotonic() + self.PENDING_ADDRESS_TTL,
            )
            await self._edit(chat_id, message_id, format_setting_prompt(field, network))
            return
        from dataclasses import replace
        if field == "suspicious":
            updated = replace(prefs, suspicious_only=not prefs.suspicious_only)
        elif field == "weekly":
            updated = replace(prefs, weekly_report=not prefs.weekly_report)
        elif field == "fee":
            updated = replace(prefs, fee_alerts=not prefs.fee_alerts)
        else:
            updated = replace(prefs, quiet_start=None, quiet_end=None)
        try:
            self.user_state.save_settings(user_id, network, address, updated)
        except Exception:
            logger.warning("Cüzdan ayarları kaydedilemedi.")
            await self._edit(chat_id, message_id, format_error(
                "Ayar kaydedilemedi. Mevcut izleme sürüyor.", main_menu_keyboard(),
            ))
            return
        await self._edit(chat_id, message_id, format_wallet_settings(address, network, updated))

    async def _accept_pending_setting(self, user_id: str, chat_id: str, text: str, pending: PendingSetting) -> None:
        if time.monotonic() >= pending.expires_at or self.user_state is None:
            self._pending_settings.pop(user_id, None)
            await self._edit(pending.chat_id, pending.message_id, format_add_expired())
            return
        if chat_id != pending.chat_id or not self._owns_wallet(user_id, pending.network, pending.address):
            self._pending_settings.pop(user_id, None)
            await self._edit(pending.chat_id, pending.message_id, format_stale_menu())
            return
        try:
            prefs = self.user_state.settings(user_id, pending.network, pending.address)
            from dataclasses import replace
            if pending.field == "alias":
                alias = "" if text == "-" else parse_alias(text)
                prefs = replace(prefs, alias=alias)
            elif pending.field == "amount":
                prefs = replace(prefs, min_amount=parse_min_amount(text))
            elif pending.field == "quiet":
                parsed = parse_quiet_hours(text)
                if parsed is None:
                    prefs = replace(prefs, quiet_start=None, quiet_end=None)
                else:
                    prefs = replace(prefs, quiet_start=parsed[0], quiet_end=parsed[1])
            else:
                raise SettingsInputError("Bu ayar değiştirilemedi.")
        except SettingsInputError as exc:
            await self._edit(pending.chat_id, pending.message_id, format_error(str(exc), cancel_keyboard()))
            return
        self.user_state.save_settings(user_id, pending.network, pending.address, prefs)
        self._pending_settings.pop(user_id, None)
        await self._edit(
            pending.chat_id,
            pending.message_id,
            format_wallet_settings(pending.address, pending.network, prefs),
        )

    async def _handle_feedback_callback(self, callback_id: str, data: str, message, sender) -> None:
        parsed = parse_feedback_callback(data)
        chat = message.get("chat") if isinstance(message, dict) else None
        private = (
            isinstance(chat, dict)
            and isinstance(sender, dict)
            and chat.get("type") == "private"
            and str(chat.get("id")) == str(sender.get("id"))
        )
        if not private or parsed is None or self.user_state is None:
            await self._answer_feedback(callback_id, "Bu geri bildirim geçersiz.")
            return
        kind, token = parsed
        user_id = str(sender.get("id"))
        try:
            record = self.user_state.feedback(token)
        except Exception:
            logger.warning("Geri bildirim kaydı okunamadı.")
            await self._answer_feedback(callback_id, "Bu geri bildirim geçersiz.")
            return
        if record is None:
            await self._answer_feedback(callback_id, "Bu geri bildirim geçersiz.")
            return
        if record.user_id != user_id or not self._owns_wallet(user_id, record.network, record.wallet_address):
            await self._answer_feedback(callback_id, "Bu işlem için yetkin yok.")
            return
        try:
            result = self.user_state.set_feedback(token, user_id, kind)
        except Exception:
            logger.warning("Geri bildirim kaydedilemedi.")
            await self._answer_feedback(callback_id, "Geri bildirim kaydedilemedi.")
            return
        if result == "unchanged":
            await self._answer_feedback(callback_id, "Geri bildirimin zaten kayıtlı.", show_alert=True)
            await self._show_feedback_choice(message, kind)
        elif result == "updated":
            await self._answer_feedback(callback_id, "Geri bildirimin kaydedildi.", show_alert=True)
            await self._show_feedback_choice(message, kind)
        elif result == "unauthorized":
            await self._answer_feedback(callback_id, "Bu işlem için yetkin yok.")
        else:
            await self._answer_feedback(callback_id, "Bu geri bildirim geçersiz.")

    async def _show_feedback_choice(self, message, kind: str) -> None:
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        message_id = message.get("message_id")
        if not isinstance(chat, dict) or not isinstance(message_id, int):
            return
        chat_id = chat.get("id")
        markup = mark_selected_feedback(message.get("reply_markup"), kind)
        original = message.get("text")
        updated_text = with_feedback_status(original, kind) if isinstance(original, str) else None
        base = without_feedback_status(original) if isinstance(original, str) else ""
        edit_text = getattr(self.notifier, "edit_text", None)
        if updated_text is not None and edit_text is not None:
            try:
                await edit_text(
                    chat_id,
                    message_id,
                    updated_text,
                    markup or message.get("reply_markup") or {"inline_keyboard": []},
                    _entities_within(message.get("entities"), base),
                )
                return
            except TelegramError:
                logger.warning("Geri bildirim mesajı güncellenemedi.")
        edit_markup = getattr(self.notifier, "edit_reply_markup", None)
        if markup is None or edit_markup is None:
            return
        try:
            await edit_markup(chat_id, message_id, markup)
        except TelegramError:
            logger.warning("Geri bildirim düğmeleri güncellenemedi.")

    async def _answer_feedback(self, callback_id: str, text: str, show_alert: bool = False) -> None:
        try:
            await self.notifier.answer_callback_query(callback_id, text, show_alert=show_alert)
        except TelegramError:
            logger.warning("Geri bildirim yanıtı gönderilemedi.")

    @staticmethod
    def _wallet_activity(action: str, network: str, address: str) -> str:
        return f"👛 Cüzdan {escape(action)} · {escape(network)}\n<code>{escape(address)}</code>"

    async def _report_activity(
        self,
        sender: dict[str, Any] | None,
        body: str,
        *,
        source_chat_id: str = "",
    ) -> None:
        target = str(self.activity_log_chat_id or "").strip()
        if not target or not isinstance(sender, dict) or source_chat_id == target:
            return
        try:
            await self.notifier.send_text(
                format_activity_notice(sender, body),
                chat_id=target,
                parse_mode="HTML",
            )
        except Exception:
            logger.warning("Kullanıcı işlemi aktivite grubuna iletilemedi.")

    async def _send(self, chat_id: str, message: FormattedMessage) -> int | None:
        try:
            result = await self.notifier.send_formatted(message, chat_id)
            message_id = result.get("message_id") if isinstance(result, dict) else None
            return message_id if isinstance(message_id, int) else None
        except TelegramError:
            logger.exception("Telegram mesajı gönderilemedi.")
            return None

    async def _edit(self, chat_id: str, message_id: int, message: FormattedMessage) -> None:
        try:
            await self.notifier.edit_formatted(chat_id, message_id, message)
        except TelegramError:
            logger.exception("Telegram menü mesajı düzenlenemedi.")

    async def close(self) -> None:
        if self.session is not None and not self.session.closed:
            await self.session.close()
