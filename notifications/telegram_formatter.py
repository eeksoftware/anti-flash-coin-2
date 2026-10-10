import html
import unicodedata
from dataclasses import dataclass
from datetime import timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from analyzer.token_analyzer import TokenAnalysis
from models.transaction import TokenTransfer
from monitor.wallet_store import WalletSubscription
from notifications.telegram_keyboards import (
    InlineKeyboard,
    back_to_main_keyboard,
    cancel_keyboard,
    check_result_keyboard,
    main_menu_keyboard,
    remove_confirmation_keyboard,
    status_keyboard,
    transfer_alert_keyboard,
    wallet_added_keyboard,
    wallets_keyboard,
)


MAX_TELEGRAM_TEXT_UNITS = 4096
SEPARATOR = "━━━━━━━━━━━━━━━━━━━━"
CLASSIFICATION_STYLE = {
    "CANONICAL": ("🟢", "Güvenli token"),
    "VARIANT": ("🟡", "Şüpheli token"),
    "KNOWN_FRAUD": ("🔴", "Sahte token"),
    "UNKNOWN": ("⚪", "Bilinmeyen token"),
}
FRAUD_WARNING = "Bu token resmî USDT kontratıyla eşleşmiyor. İşlem yapmadan önce kontratı doğrula."
_DIRECTION_ROWS = {
    "RECEIVE": "⬇️ <b>Alındı</b>",
    "SEND": "⬆️ <b>Gönderildi</b>",
}


@dataclass(frozen=True)
class FormattedMessage:
    text: str
    parse_mode: str = "HTML"
    reply_markup: InlineKeyboard | None = None


def escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _with_separator(title: str, body: str) -> str:
    return f"{title}\n{SEPARATOR}\n{body}"


def sanitize_token_text(value: Any, limit: int = 20) -> str:
    """Remove control and bidi formatting characters from token supplied text."""
    bidi_controls = {
        "\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c",
        "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069",
    }
    cleaned = "".join(
        char for char in str(value)
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
        and char not in bidi_controls
    )
    return cleaned[:limit]


def format_token_amount(amount: Decimal) -> str:
    rendered = f"{amount:,f}"
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def format_usd(amount: Decimal, price: Decimal | None) -> str | None:
    if amount == 0:
        return "$0.00"
    if price is None:
        return None
    value = amount * price
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded == 0 and value != 0:
        return "<$0.01"
    return f"${rounded:,.2f}"


def format_start(
    bot_name: str,
    first_name: str,
    wallet_count: int,
) -> FormattedMessage:
    return FormattedMessage(
        text=(
            # f"🤖 <b>{escape(bot_name or 'Anti-Flash Token Bot')}</b>\n\n"
            _with_separator(
                "🤖 <b>Anti-Flash Token Bot</b>",
                f"Merhaba, <b>{escape(first_name or 'kullanıcı')}</b>! 👋\n"
                "Cüzdanlarını izler, sahte (flash) token transferlerini bildirir.\n\n"
                "🛡️ Resmî token kontratlarını doğrular\n"
                "🔔 Şüpheli transferlerde seni uyarır\n"
                f"👛 İzlediğin cüzdan: <b>{wallet_count}</b>",
            )
        ),
        reply_markup=main_menu_keyboard(),
    )


def format_group_start(bot_username: str | None) -> FormattedMessage:
    if bot_username:
        from notifications.telegram_keyboards import private_chat_keyboard

        return FormattedMessage(
            _with_separator(
                "ℹ️ <b>Özel Sohbet</b>",
                "Cüzdanlarını yönetmek için botu özel mesajda başlat.",
            ),
            reply_markup=private_chat_keyboard(bot_username),
        )
    return FormattedMessage(_with_separator(
        "ℹ️ <b>Özel Sohbet</b>",
        "Cüzdanlarını yönetmek için botu özel mesajda başlat.",
    ))


def format_help() -> FormattedMessage:
    commands = [
        "➕ <b>/watch &lt;ağ&gt; &lt;adres&gt;</b> — cüzdan ekle\n",
        "📋 <b>/wallets</b> — izlenen cüzdanları listele\n",
        "🗑️ <b>/unwatch &lt;ağ&gt; &lt;adres&gt;</b> — cüzdanı kaldır\n",
        "🕘 <b>/history &lt;ağ&gt; &lt;adres&gt;</b> — son 10 ERC-20 transferini göster\n",
        "🔍 <b>/check [ağ] &lt;değer&gt;</b> — token ya da işlem kontrol et\n",
        "📊 <b>/status</b> — izleme durumunu göster",
    ]
    return FormattedMessage(
        _with_separator("❓ <b>Yardım</b>", "\n".join(commands)),
        reply_markup=back_to_main_keyboard(),
    )


def format_about() -> FormattedMessage:
    return FormattedMessage(
        _with_separator(
            "ℹ️ <b>Hakkında</b>",
            "🛡️ Bu bot, Ethereum cüzdanlarındaki token transferlerini senin yerine takip eder.\n\n"
            '🔍 Her transfer, göründüğü gibi değil gerçekte ne olduğuna göre incelenir. Sahte ya da "flash" olduğundan şüphelenilen bir token fark edildiğinde sana anında haber verir.\n\n'
            "🔔 Tek yapman gereken cüzdanını eklemek. Gerisini bot halleder.\n\n"
            "⚠️ Bot bir yardımcıdır. Yüksek tutarlı işlemlerde son kontrolü her zaman kendin yap.",
        ),
        reply_markup=back_to_main_keyboard(),
    )


def format_status(wallets: list[WalletSubscription]) -> FormattedMessage:
    return FormattedMessage(
        _with_separator(
            "📊 <b>İzleme Durumu</b>",
            f"🌐 Ağ: <b>Ethereum</b>\n👛 İzlenen cüzdan: <b>{len(wallets)}</b>",
        ),
        reply_markup=status_keyboard(),
    )


def format_wallet_list(
    wallets: list[WalletSubscription],
    page: int = 0,
    page_size: int = 8,
) -> FormattedMessage:
    if not wallets:
        body = "ℹ️ Henüz izlenen cüzdan yok."
    else:
        start = max(page, 0) * page_size
        current = wallets[start : start + page_size]
        wallet_lines = [
            f"<b>{index}.</b> {escape(wallet.network.capitalize())} · <code>{escape(wallet.wallet_address)}</code>"
            for index, wallet in enumerate(current, start=start + 1)
        ]
        body = "\n".join(wallet_lines)
    return FormattedMessage(
        _with_separator("📋 <b>Cüzdanlarım</b>", body),
        reply_markup=wallets_keyboard(wallets, page=page, page_size=page_size),
    )


def format_add_network() -> FormattedMessage:
    from notifications.telegram_keyboards import add_network_keyboard

    return FormattedMessage(
        _with_separator("➕ <b>Cüzdan Ekle</b>", "🌐 İzlenecek ağı seç:"),
        reply_markup=add_network_keyboard(),
    )


def format_add_prompt(network: str) -> FormattedMessage:
    return FormattedMessage(
        _with_separator(
            f"➕ <b>{escape(network.capitalize())} cüzdanı ekle</b>",
            "İzlenecek cüzdan adresini gönder (0x ile başlamalı).\n"
            "⏳ Bu istek 5 dakika geçerlidir.",
        ),
        reply_markup=cancel_keyboard(),
    )


def format_add_invalid() -> FormattedMessage:
    return FormattedMessage(
        _with_separator(
            "❌ <b>Geçersiz adres</b>",
            "Geçerli bir Ethereum 0x cüzdan adresi gönder.\nAdres bekleme süresi devam ediyor.",
        ),
        reply_markup=cancel_keyboard(),
    )


def format_add_expired() -> FormattedMessage:
    return FormattedMessage(
        _with_separator(
            "⚠️ <b>İstek zaman aşımına uğradı</b>",
            "Cüzdan ekleme isteğinin süresi doldu. Yeniden başlamak için /start yaz.",
        ),
        reply_markup=main_menu_keyboard(),
    )


def format_watch_result(address: str, added: bool) -> FormattedMessage:
    if added:
        title = "✅ <b>Cüzdan izlemeye eklendi</b>"
        body = f"🌐 Ethereum\n👛 <code>{escape(address.lower())}</code>"
    else:
        title = "ℹ️ <b>Cüzdan zaten izleniyor</b>"
        body = f"👛 <code>{escape(address.lower())}</code>"
    return FormattedMessage(_with_separator(title, body), reply_markup=wallet_added_keyboard())


def format_watch_error(message: str) -> FormattedMessage:
    return FormattedMessage(
        _with_separator("❌ <b>Hata</b>", escape(message)),
        reply_markup=main_menu_keyboard(),
    )


def format_remove_confirmation(address: str) -> FormattedMessage:
    return FormattedMessage(
        _with_separator(
            "⚠️ <b>Cüzdanı kaldır?</b>",
            f"<code>{escape(address.lower())}</code>",
        ),
        reply_markup=remove_confirmation_keyboard(address),
    )


def format_remove_result(address: str, removed: bool) -> FormattedMessage:
    if removed:
        return FormattedMessage(
            _with_separator(
                "✅ <b>Cüzdan izleme listesinden kaldırıldı</b>",
                f"<code>{escape(address.lower())}</code>",
            ),
            reply_markup=back_to_main_keyboard(),
        )
    return FormattedMessage(
        _with_separator(
            "⚠️ <b>Cüzdan izleme listende bulunamadı</b>",
            f"<code>{escape(address.lower())}</code>",
        ),
        reply_markup=back_to_main_keyboard(),
    )


def _direction_row(transfer: TokenTransfer) -> str:
    return _DIRECTION_ROWS.get(transfer.direction, _DIRECTION_ROWS["SEND"])


def _party_rows(transfer: TokenTransfer) -> tuple[str, str]:
    return (
        f"📤 Gönderen: <code>{escape(transfer.sender)}</code>",
        f"📥 Alıcı: <code>{escape(transfer.receiver)}</code>",
    )


def _amount_row(transfer: TokenTransfer) -> str:
    symbol = sanitize_token_text(transfer.token_symbol)
    return (
        f"🪙 Miktar: <b>{escape(format_token_amount(transfer.amount))} "
        f"<code>{escape(symbol)}</code></b>"
    )


def _time_row(transfer: TokenTransfer) -> str:
    timestamp = transfer.timestamp.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    return f"🕒 {escape(timestamp)}"


def format_stale_menu() -> FormattedMessage:
    return FormattedMessage(
        _with_separator(
            "⚠️ <b>Bu menü eski</b>",
            "Yeniden açmak için /start yaz.",
        ),
        reply_markup=back_to_main_keyboard(),
    )


def format_transfer_alert(
    transfer: TokenTransfer,
    analysis: TokenAnalysis,
) -> FormattedMessage:
    severity, classification = CLASSIFICATION_STYLE.get(
        analysis.classification, ("⚪", "Bilinmeyen token")
    )
    name = sanitize_token_text(transfer.token_name)
    rows = [
        f"{severity} <b>Token Transferi · {escape(classification)}</b>",
        SEPARATOR,
        _time_row(transfer),
        _direction_row(transfer),
        _amount_row(transfer),
    ]
    if name:
        rows.append(f"📄 Token: <code>{escape(name)}</code>")
    rows.extend([
        f"🌐 Ağ: {escape(transfer.network.capitalize())}",
        f"🛡️ Sınıflandırma: <b>{escape(classification)}</b>",
    ])
    if analysis.classification in {"VARIANT", "KNOWN_FRAUD"}:
        rows.append(f"⚠️ {escape(FRAUD_WARNING)}")
    if analysis.details:
        rows.append(f"ℹ️ Ayrıntı: {escape(analysis.details)}")
    rows.extend([
        *_party_rows(transfer),
        f"📄 Token kontratı: <code>{escape(transfer.token_address)}</code>",
        f"🔗 TXID: <code>{escape(transfer.tx_hash)}</code>",
    ])
    wallet_address = transfer.receiver if transfer.direction == "RECEIVE" else transfer.sender
    return FormattedMessage(
        "\n".join(rows),
        reply_markup=transfer_alert_keyboard(
            transfer.network,
            transfer.tx_hash,
            transfer.token_address,
            wallet_address,
        ),
    )


def _history_entry(
    transfer: TokenTransfer,
    price: Decimal | None,
) -> str:
    usd = format_usd(transfer.amount, price)
    rows = [
        SEPARATOR,
        _time_row(transfer),
        _direction_row(transfer),
        _amount_row(transfer),
    ]
    if usd is not None:
        rows.append(f"💵 USD değeri: <b>{escape(usd)}</b>")
    rows.extend([
        *_party_rows(transfer),
        f"🔗 TXID: <code>{escape(transfer.tx_hash)}</code>",
    ])
    return "\n".join(rows)


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def format_history(
    transfers: Iterable[TokenTransfer],
    prices: dict[str, Decimal],
    wallet_address: str,
    reply_markup: InlineKeyboard,
    max_units: int = MAX_TELEGRAM_TEXT_UNITS,
) -> list[FormattedMessage]:
    transfer_list = list(transfers)
    header = (
        f"🕘 <b>Son {len(transfer_list)} ERC-20 Transferi</b>\n"
        f"{SEPARATOR}\n"
        f"👛 Cüzdan: <code>{escape(wallet_address)}</code>\n"
        "🌐 Ağ: Ethereum"
    )
    chunks: list[str] = []
    current = header
    for transfer in transfer_list:
        entry = "\n" + _history_entry(
            transfer,
            prices.get(transfer.token_address.lower()),
        )
        if _utf16_units(current + entry) > max_units and current != header:
            chunks.append(current)
            current = header + entry
        else:
            current += entry
        if _utf16_units(current) > max_units:
            raise ValueError("Tek bir transfer kaydı Telegram mesaj sınırını aşıyor.")
    chunks.append(current)
    return [
        FormattedMessage(
            chunk,
            reply_markup=reply_markup if index == len(chunks) - 1 else None,
        )
        for index, chunk in enumerate(chunks)
    ]


def format_history_empty(
    wallet_address: str,
    reply_markup: InlineKeyboard,
) -> FormattedMessage:
    return FormattedMessage(
        _with_separator(
            "📄 <b>Transfer Geçmişi</b>",
            f"ℹ️ Bu cüzdan için ERC-20 transferi bulunamadı.\n👛 <code>{escape(wallet_address)}</code>",
        ),
        reply_markup=reply_markup,
    )


def format_history_selection(wallets: list[WalletSubscription]) -> FormattedMessage:
    from notifications.telegram_keyboards import history_wallets_keyboard

    return FormattedMessage(
        _with_separator("🕘 <b>Geçmiş</b>", "Cüzdan seç:"),
        reply_markup=history_wallets_keyboard(wallets),
    )


def format_error(message: str, reply_markup: InlineKeyboard | None = None) -> FormattedMessage:
    return FormattedMessage(
        _with_separator("❌ <b>Hata</b>", escape(message)),
        reply_markup=reply_markup,
    )


def format_api_status(status: str) -> FormattedMessage:
    messages = {
        "API_UNAVAILABLE": ("⚠️ <b>Bağlantı uyarısı</b>", "Blokzincir API'sine en az 30 saniyedir erişilemiyor."),
        "API_RECOVERED": ("✅ <b>Bağlantı düzeldi</b>", "Blokzincir API bağlantısı yeniden kuruldu."),
    }
    if status not in messages:
        raise ValueError(f"Desteklenmeyen API durumu: {status}")
    title, body = messages[status]
    return FormattedMessage(_with_separator(title, body))


def format_check_usage() -> FormattedMessage:
    return FormattedMessage(
        _with_separator(
            "🔍 <b>Token ya da işlem kontrol et</b>",
            "Bir token kontratı veya işlem hash'i gönder. Ağ belirtilmezse Ethereum kullanılır.\n\n"
            "Örnekler:\n<code>/check 0x0000000000000000000000000000000000000000</code>\n"
            "<code>/check ethereum 0x0000000000000000000000000000000000000000000000000000000000000000</code>",
        ),
        reply_markup=back_to_main_keyboard(),
    )


def format_check_prompt() -> FormattedMessage:
    return FormattedMessage(
        _with_separator(
            "🔍 <b>Kontrol Et</b>",
            "Token kontratı ya da işlem hash'i gönder.\n⏳ Bu istek 5 dakika geçerlidir.",
        ),
        reply_markup=cancel_keyboard(),
    )


def format_check_error(kind: str = "provider") -> FormattedMessage:
    messages = {
        "not_found": "İşlem bulunamadı. Henüz ağa gönderilmemiş ya da ağ yanlış olabilir.",
        "rate_limit": "⏳ Etherscan istek sınırına ulaşıldı. Biraz sonra yeniden dene.",
        "provider": "Blokzincir sağlayıcısından yanıt alınamadı. Biraz sonra yeniden dene.",
        "too_fast": "⏳ Çok hızlı; birkaç saniye bekle.",
        "expired": "Kontrol isteğinin süresi doldu. Yeniden başlamak için /check yaz.",
    }
    return FormattedMessage(
        _with_separator("⚠️ <b>Kontrol tamamlanamadı</b>", messages.get(kind, messages["provider"])),
        reply_markup=main_menu_keyboard(),
    )


def format_token_check(result) -> FormattedMessage:
    if result.is_wallet:
        return FormattedMessage(
            _with_separator(
                "👛 <b>Cüzdan adresi</b>",
                "Bu bir cüzdan adresi, token kontratı değil. /watch ile izleyebilir veya /history ile geçmişine bakabilirsin.\n\n"
                f"📄 Adres: <code>{escape(result.token_address)}</code>",
            ),
            reply_markup=check_result_keyboard(result.network, result.token_address, token=True, wallet=True),
        )
    analysis = result.analysis
    classification = analysis.classification if analysis is not None else "UNKNOWN"
    severity, label = CLASSIFICATION_STYLE.get(classification, CLASSIFICATION_STYLE["UNKNOWN"])
    info = result.contract_info
    symbol = sanitize_token_text(info.symbol or "Bilinmiyor")
    name = sanitize_token_text(info.name or "").replace("\n", " ")
    rows = [
        f"{severity} <b>Token Kontrolü · {escape(label)}</b>",
        SEPARATOR,
        f"🌐 Ağ: {escape(result.network.capitalize())}",
        f"🪙 Token: <code>{escape(symbol)}</code>" + (f" ({escape(name)})" if name else ""),
        f"📄 Token kontratı: <code>{escape(result.token_address)}</code>",
        f"🛡️ Sınıflandırma: <b>{escape(label)}</b>",
    ]
    if classification in {"VARIANT", "KNOWN_FRAUD"}:
        rows.append(f"⚠️ {escape(FRAUD_WARNING)}")
    if analysis and analysis.details:
        detail = sanitize_token_text(analysis.details, 500).replace("\n", " ")
        rows.append(f"ℹ️ Ayrıntı: {escape(detail)}")
    return FormattedMessage(
        _fit_check_text("\n".join(rows)),
        reply_markup=check_result_keyboard(
            result.network, result.token_address, token=True, suspicious=classification in {"VARIANT", "KNOWN_FRAUD"}
        ),
    )


def format_tx_check(result) -> FormattedMessage:
    from notifications.telegram_keyboards import url_button
    from networks import get_network_config

    if result.transaction is None:
        return format_check_error("not_found")
    config = get_network_config(result.network)
    classification = result.classification
    severity, label = CLASSIFICATION_STYLE.get(classification, CLASSIFICATION_STYLE["UNKNOWN"])
    if not result.transfers:
        label = "Token transferi yok"
    transaction = result.transaction
    status = {"success": "✅ Başarılı", "failed": "❌ Başarısız", "pending": "⏳ Beklemede"}.get(transaction.status, "⏳ Beklemede")
    rows = [
        f"{severity} <b>İşlem Kontrolü · {escape(label)}</b>",
        SEPARATOR,
        f"🌐 Ağ: {escape(result.network.capitalize())}",
        f"🔗 TXID: <code>{escape(result.tx_hash)}</code>",
        status,
    ]
    if transaction.block_time:
        moment = transaction.block_time.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        rows.append(f"🕒 {escape(moment)}")
    if transaction.block_number is not None:
        block_row = f"🧱 Blok: {transaction.block_number:,}"
        if transaction.confirmations is not None:
            block_row += f" · Onay: {transaction.confirmations:,}"
        rows.append(block_row)
    suspicious_address = next(
        (
            item.transfer.token_address
            for item in result.transfers
            if item.analysis.classification in {"VARIANT", "KNOWN_FRAUD"}
        ),
        None,
    )
    if not result.transfers:
        rows.append("ℹ️ Bu işlemde ERC-20 token transferi bulunamadı.")
    visible = list(result.transfers[:5])
    for index, item in enumerate(visible, 1):
        transfer = item.transfer
        style, item_label = CLASSIFICATION_STYLE.get(item.analysis.classification, CLASSIFICATION_STYLE["UNKNOWN"])
        symbol = sanitize_token_text(transfer.token_symbol)
        rows.extend([
            SEPARATOR,
            f"{style} <b>Token transferi {index} · {escape(item_label)}</b>",
            f"🪙 Miktar: <b>{escape(format_token_amount(transfer.amount))} <code>{escape(symbol)}</code></b>",
            f"🛡️ Sınıflandırma: <b>{escape(item_label)}</b>",
            f"📤 Gönderen: <code>{escape(transfer.sender)}</code>",
            f"📥 Alıcı: <code>{escape(transfer.receiver)}</code>",
            f"📄 Token kontratı: <code>{escape(transfer.token_address)}</code>",
        ])
    if len(result.transfers) > len(visible):
        rows.append(f"+{len(result.transfers) - len(visible)} transfer daha")
    buttons = [[url_button("🔍 İşlemi Aç", config.transaction_url(result.tx_hash))]]
    if suspicious_address:
        buttons[0].append(url_button("📄 Kontratı Aç", config.token_url(suspicious_address)))
    text = _fit_check_text("\n".join(rows))
    if len(text.encode("utf-16-le")) // 2 > MAX_TELEGRAM_TEXT_UNITS:
        text = _fit_check_text("\n".join(rows[:6] + ["ℹ️ Ayrıntılar kısaltıldı."]))
    return FormattedMessage(text, reply_markup={"inline_keyboard": buttons})


def _fit_check_text(text: str) -> str:
    if _utf16_units(text) <= MAX_TELEGRAM_TEXT_UNITS:
        return text
    # Only truncate plain text after escaping so HTML tags remain balanced.
    suffix = "…"
    budget = MAX_TELEGRAM_TEXT_UNITS - _utf16_units(suffix)
    output = []
    used = 0
    for char in text:
        units = _utf16_units(char)
        if used + units > budget:
            break
        output.append(char)
        used += units
    clipped = "".join(output)
    # Back off to a complete line if truncation landed inside HTML markup.
    clipped = clipped.rsplit("\n", 1)[0] if "\n" in clipped else ""
    return clipped + suffix
