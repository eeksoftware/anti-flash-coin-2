import html
import unicodedata
from dataclasses import dataclass
from datetime import timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from analyzer.token_analyzer import TokenAnalysis
from models.transaction import TokenTransfer
from monitor.wallet_store import WalletSubscription
from networks import get_network_config
from notifications.telegram_keyboards import (
    InlineKeyboard,
    approval_alert_keyboard,
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
    """Remove characters that make Telegram replace the whole message.

    Control, format, surrogate and nonspacing marks are dropped. Visible
    letters stay, including a Greek tau that only looks like a Latin T.
    """
    bidi_controls = {
        "\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c",
        "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069",
    }
    cleaned = "".join(
        char for char in str(value)
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Mn", "Me"}
        and char not in bidi_controls
    )
    return cleaned[:limit]


def display_token_text(value: Any, limit: int = 20) -> str:
    """Token symbol or name safe to send through Telegram."""
    raw = str(value or "")
    if not raw.strip():
        return ""
    return sanitize_token_text(raw, limit=max(len(raw), limit))[:limit]


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
        "📊 <b>/status</b> — izleme durumunu göster\n",
        "📤 <b>/export</b> — CSV dışa aktar. Örnek: <code>/export 7d</code>\n",
        "⚙️ Cüzdan ayarları, Cüzdanlarım listesindeki Ayarlar düğmesindedir. Saatler UTC'dir.",
    ]
    return FormattedMessage(
        _with_separator("❓ <b>Yardım</b>", "\n".join(commands)),
        reply_markup=back_to_main_keyboard(),
    )


def format_health_report(report: dict[str, Any]) -> FormattedMessage:
    icon = str(report.get("icon", "🟡"))
    lines = [
        f"{icon} <b>Sağlık Durumu · {escape(report.get('label', 'Uyarı'))}</b>",
        SEPARATOR,
        f"🤖 Sürüm: <code>{escape(report.get('version', 'unknown'))}</code> · Commit: <code>{escape(report.get('commit', 'unknown'))}</code>",
        f"⏱️ Uptime: {escape(report.get('uptime', 'N/A'))} · İzlenen cüzdan: {escape(report.get('wallet_count', 'N/A'))}",
        f"🔄 Son izleme: {escape(report.get('last_cycle', 'N/A'))} · Döngü: {escape(report.get('cycle_count', 'N/A'))} · Aralık: {escape(report.get('interval', 'N/A'))}",
        f"⚠️ Son hata: {escape(report.get('last_error', 'Yok'))}",
        f"🌐 API son başarı: {escape(report.get('last_api', 'N/A'))} · Telegram son başarı: {escape(report.get('last_telegram', 'N/A'))}",
        f"💾 Log boyutu: {escape(report.get('log_size', 'N/A'))} · Disk boş: {escape(report.get('disk_free', 'N/A'))}",
        f"🔁 Son 7 gün beklenmeyen kapanış: {escape(report.get('unexpected_restarts', 'N/A'))}",
        SEPARATOR,
    ]
    keys = report.get("keys", [])
    if keys:
        lines.extend(f"🔑 {escape(row.get('label'))}: {escape(row.get('state'))} · {escape(row.get('usage'))}" for row in keys)
    else:
        lines.append("🔑 API key durumu: N/A")
    return FormattedMessage("\n".join(lines))


def format_stats_report(report: dict[str, Any]) -> FormattedMessage:
    lines = ["📊 <b>İstatistikler</b>", SEPARATOR]
    for period, title in (("today", "Bugün · UTC"), ("last24h", "Son 24 saat")):
        data = report.get(period, {})
        lines.extend([f"🕒 <b>{title}</b>",
            f"Transfer: {escape(data.get('transfer_total', 'N/A'))} · Şüpheli/sahte: {escape(data.get('suspicious_total', 'N/A'))}",
            f"Bildirim: {escape(data.get('notifications_sent', 'N/A'))} · Hata: {escape(data.get('errors', 'N/A'))} · /check: {escape(data.get('check_usage', 'N/A'))}"])
        classes = data.get("classes", [])
        lines.extend(f"  {escape(row['name'])}: {escape(row['count'])} (%{escape(row['percent'])})" for row in classes)
        lines.append(SEPARATOR)
    api = report.get("api", {})
    lines.append("🌐 <b>API</b>")
    lines.append(f"Rate limit: {escape(api.get('rate_limited', 'N/A'))} · Ortalama: {escape(api.get('latency_avg', 'N/A'))} · p95: {escape(api.get('latency_p95', 'N/A'))} · Cache hit: {escape(api.get('cache_hit_ratio', 'N/A'))}")
    lines.extend(f"🔑 {escape(row.get('label'))}: {escape(row.get('calls'))}/{escape(row.get('limit'))} (%{escape(row.get('percent'))})" for row in api.get("keys", []))
    for network, provider in api.get("providers", {}).items():
        if network == api.get("primary_network", "ethereum"):
            continue
        lines.append(f"🌐 <b>{escape(get_network_config(network).display_name)} · API</b>")
        lines.append(f"Rate limit: {escape(provider.get('rate_limited', 'N/A'))} · Ortalama: {escape(provider.get('latency_avg', 'N/A'))} · p95: {escape(provider.get('latency_p95', 'N/A'))} · Cache hit: {escape(provider.get('cache_hit_ratio', 'N/A'))}")
        lines.extend(f"🔑 {escape(row.get('label'))}: {escape(row.get('calls'))}/{escape(row.get('limit'))} (%{escape(row.get('percent'))})" for row in provider.get("keys", []))
    return FormattedMessage("\n".join(lines))


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
    aliases: dict[tuple[str, str], str] | None = None,
) -> FormattedMessage:
    if not wallets:
        body = "ℹ️ Henüz izlenen cüzdan yok."
    else:
        start = max(page, 0) * page_size
        current = wallets[start : start + page_size]
        wallet_lines = []
        for index, wallet in enumerate(current, start=start + 1):
            line = f"<b>{index}.</b> {escape(wallet.network.capitalize())} · <code>{escape(wallet.wallet_address)}</code>"
            alias = (aliases or {}).get((wallet.network, wallet.wallet_address), "")
            if alias:
                line += f" · {escape(alias)}"
            wallet_lines.append(line)
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
    config = get_network_config(network)
    address_hint = "TRON Base58 adresi (T ile başlayan) gönder.\n" if network == "tron" else "İzlenecek cüzdan adresini gönder (0x ile başlamalı).\n"
    return FormattedMessage(
        _with_separator(
            f"➕ <b>{escape(config.display_name)} cüzdanı ekle</b>",
            address_hint +
            "⏳ Bu istek 5 dakika geçerlidir.",
        ),
        reply_markup=cancel_keyboard(),
    )


def format_add_invalid(network: str = "ethereum") -> FormattedMessage:
    hint = "Geçerli bir TRON Base58 adresi gönder.\nAdres büyük/küçük harfe duyarlıdır.\n" if network == "tron" else "Geçerli bir Ethereum 0x cüzdan adresi gönder.\n"
    return FormattedMessage(
        _with_separator(
            "❌ <b>Geçersiz adres</b>",
            hint + "Adres bekleme süresi devam ediyor.",
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


def format_watch_result(address: str, added: bool, network: str = "ethereum") -> FormattedMessage:
    from networks import get_network_config
    config = get_network_config(network)
    address = config.normalize_address(address)
    address_row = f"👛 <code>{escape(address)}</code>"
    if added:
        title = "✅ <b>Cüzdan izlemeye eklendi</b>"
        body = f"🌐 {escape(config.display_name)}\n{address_row}"
    else:
        title = "ℹ️ <b>Cüzdan zaten izleniyor</b>"
        network_prefix = f"🌐 {escape(config.display_name)}\n" if config.family == "tron" else ""
        body = f"{network_prefix}{address_row}"
    return FormattedMessage(_with_separator(title, body), reply_markup=wallet_added_keyboard())


def format_watch_error(message: str) -> FormattedMessage:
    return FormattedMessage(
        _with_separator("❌ <b>Hata</b>", escape(message)),
        reply_markup=main_menu_keyboard(),
    )


def format_remove_confirmation(address: str, network: str = "ethereum") -> FormattedMessage:
    from networks import get_network_config
    address = get_network_config(network).normalize_address(address)
    return FormattedMessage(
        _with_separator(
            "⚠️ <b>Cüzdanı kaldır?</b>",
            f"<code>{escape(address)}</code>",
        ),
        reply_markup=remove_confirmation_keyboard(address, network),
    )


def format_remove_result(address: str, removed: bool, network: str = "ethereum") -> FormattedMessage:
    from networks import get_network_config
    address = get_network_config(network).normalize_address(address)
    if removed:
        return FormattedMessage(
            _with_separator(
                "✅ <b>Cüzdan izleme listesinden kaldırıldı</b>",
                f"<code>{escape(address)}</code>",
            ),
            reply_markup=back_to_main_keyboard(),
        )
    return FormattedMessage(
        _with_separator(
            "⚠️ <b>Cüzdan izleme listende bulunamadı</b>",
            f"<code>{escape(address)}</code>",
        ),
        reply_markup=back_to_main_keyboard(),
    )


def _direction_row(transfer: TokenTransfer) -> str:
    return _DIRECTION_ROWS.get(transfer.direction, _DIRECTION_ROWS["SEND"])


def _party_rows(transfer: TokenTransfer, address_names: dict[str, str] | None = None) -> tuple[str, str]:
    return (
        _address_line("📤 Gönderen", transfer.sender, address_names),
        _address_line("📥 Alıcı", transfer.receiver, address_names),
    )


def _address_line(label: str, address: str, address_names: dict[str, str] | None = None) -> str:
    line = f"{label}: <code>{escape(address)}</code>"
    tag = (address_names or {}).get(str(address or "").lower(), "")
    if tag:
        line += f" <b>{escape(tag)}</b>"
    return line


def _amount_row(transfer: TokenTransfer) -> str:
    symbol = display_token_text(transfer.token_symbol)
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
    *,
    alias: str | None = None,
    feedback_token: str | None = None,
    risk=None,
    poisoning=None,
    address_names: dict[str, str] | None = None,
) -> FormattedMessage:
    severity, classification = CLASSIFICATION_STYLE.get(
        analysis.classification, ("⚪", "Bilinmeyen token")
    )
    name = display_token_text(transfer.token_name)
    rows = [
        f"{severity} <b>Token Transferi · {escape(classification)}</b>",
        SEPARATOR,
        _time_row(transfer),
        _direction_row(transfer),
        _amount_row(transfer),
    ]
    if name:
        rows.append(f"📄 Token: <code>{escape(name)}</code>")
    if alias:
        rows.append(f"👛 Takma ad: <code>{escape(alias)}</code>")
    rows.extend([
        f"🌐 Ağ: {escape(get_network_config(transfer.network).display_name)}",
        f"🛡️ Sınıflandırma: <b>{escape(classification)}</b>",
    ])
    if analysis.classification in {"VARIANT", "KNOWN_FRAUD"}:
        rows.append(f"⚠️ {escape(FRAUD_WARNING)}")
    if analysis.details:
        rows.append(f"ℹ️ Ayrıntı: {escape(analysis.details)}")
    if poisoning is not None:
        rows.append(
            "🎭 Adres benzerliği (şüphe): karşı taraf daha önce etkileşilen "
            f"<code>{escape(poisoning.known_address)}</code> adresine benziyor. Adresi tam olarak doğrulayın."
        )
    rows.extend(_risk_rows(risk))
    rows.extend([
        *_party_rows(transfer, address_names),
        _address_line("📄 Token kontratı", transfer.token_address, address_names),
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
            feedback_token,
        ),
    )


def _risk_rows(risk) -> list[str]:
    if risk is None:
        return []
    rows = [f"{risk.icon} Risk skoru: <b>{risk.score}/100 · {escape(risk.level)}</b>"]
    for signal in risk.signals[:3]:
        marker = "" if signal.verified else " (doğrulanmamış)"
        rows.append(f"  • {escape(signal.evidence)}{escape(marker)}")
    if risk.missing:
        rows.append(f"  • Eksik veri: {escape(', '.join(risk.missing))}")
    return rows


_APPROVAL_STATE_TEXT = {
    "NEW": "Yeni yetki",
    "INCREASED": "Yetki artırıldı",
    "DECREASED": "Yetki azaltıldı",
    "REVOKED": "Yetki iptal edildi",
    "UNLIMITED": "Sınırsız yetki",
}


def format_approval_alert(event, assessment, risk, *, alias: str | None = None, token_symbol: str | None = None) -> FormattedMessage:
    """Approval notice. Informational only: the bot never revokes or signs anything."""
    config = get_network_config(event.network)
    state = _APPROVAL_STATE_TEXT.get(assessment.state, assessment.state)
    if assessment.human_value is not None and assessment.state != "UNLIMITED":
        amount = escape(format_token_amount(assessment.human_value))
        if token_symbol:
            amount += f" <code>{escape(display_token_text(token_symbol))}</code>"
    elif assessment.state == "UNLIMITED":
        amount = "sınırsız"
    else:
        amount = f"ham değer {escape(str(event.value_raw))} (ondalık bilinmiyor)"
    rows = [
        f"{risk.icon} <b>Token Yetkisi · {escape(state)}</b>",
        SEPARATOR,
        f"🌐 Ağ: {escape(config.display_name)}",
        f"👛 Cüzdan: <code>{escape(event.owner)}</code>",
    ]
    if alias:
        rows.append(f"👛 Takma ad: <code>{escape(alias)}</code>")
    rows.extend([
        f"🏷️ Harcayıcı: <code>{escape(event.spender)}</code>" + (f" ({escape(assessment.spender_label)})" if assessment.spender_label else ""),
        f"📄 Token kontratı: <code>{escape(event.token_address)}</code>",
        f"🪙 Yetki tutarı: <b>{amount}</b>",
    ])
    rows.extend(_risk_rows(risk))
    rows.extend([
        f"🔗 TXID: <code>{escape(event.tx_hash)}</code>",
        "ℹ️ Bu bildirim bilgilendirme amaçlıdır; bot yetki iptal etmez ve işlem imzalamaz.",
    ])
    return FormattedMessage(
        "\n".join(rows),
        reply_markup=approval_alert_keyboard(event.network, event.tx_hash, event.token_address, event.owner),
    )


def format_wallet_settings(address: str, network: str, prefs) -> FormattedMessage:
    from notifications.telegram_keyboards import wallet_settings_keyboard

    alias = prefs.alias or "Yok"
    amount = format(prefs.min_amount, "f")
    suspicious = "Açık" if prefs.suspicious_only else "Kapalı"
    if prefs.quiet_start and prefs.quiet_end and prefs.quiet_start != prefs.quiet_end:
        quiet = f"{prefs.quiet_start}–{prefs.quiet_end} UTC"
    else:
        quiet = "Yok"
    body = "\n".join([
        f"🌐 Ağ: {escape(get_network_config(network).display_name)}",
        f"👛 Cüzdan: <code>{escape(address)}</code>",
        f"✏️ Takma ad: <code>{escape(alias)}</code>",
        f"💰 Minimum tutar: <b>{escape(amount)}</b>",
        f"🚨 Yalnızca şüpheli: <b>{suspicious}</b>",
        "ℹ️ Şüpheli filtre varyant ve bilinen sahte tokenları bırakır.",
        f"🌙 Sessiz saat: <b>{escape(quiet)}</b>",
        "ℹ️ Saatler UTC'dir. Başlangıç ve bitiş aynıysa filtre kapalıdır.",
        f"🗓️ Haftalık özet: <b>{'Açık' if prefs.weekly_report else 'Kapalı'}</b>",
        "ℹ️ Haftalık özet transfer bildiriminden bağımsızdır. Kapalıyken kayıt sürer.",
        f"💸 Fee bildirimi: <b>{'Açık' if prefs.fee_alerts else 'Kapalı'}</b>",
        "ℹ️ 0,01 USD altı fee sayılır. Kapalıyken gönderilmez. Açıkken kısa fee mesajı gider. Şüpheli tespit bu filtreden geçmez.",
    ])
    return FormattedMessage(
        _with_separator("⚙️ <b>Cüzdan Ayarları</b>", body),
        reply_markup=wallet_settings_keyboard(address, network),
    )


def format_fee_alert(
    transfer: TokenTransfer,
    usd: Decimal,
    *,
    alias: str | None = None,
    address_names: dict[str, str] | None = None,
) -> FormattedMessage:
    """Short notice for a transfer worth under 0.01 USD."""
    rows = [
        "💸 <b>Fee</b>",
        SEPARATOR,
        _time_row(transfer),
        _amount_row(transfer),
        f"💵 USD: <b>{escape(format_usd(usd, Decimal(1)) or '<$0.01')}</b>",
        f"🌐 Ağ: {escape(get_network_config(transfer.network).display_name)}",
    ]
    if alias:
        rows.append(f"👛 Takma ad: <code>{escape(alias)}</code>")
    for label, address in (("📤 Gönderen", transfer.sender), ("📥 Alıcı", transfer.receiver)):
        if (address_names or {}).get(str(address or "").lower()):
            rows.append(_address_line(label, address, address_names))
    rows.extend([
        f"🔗 TXID: <code>{escape(transfer.tx_hash)}</code>",
        "ℹ️ 0,01 USD altı. Kayıt durur; bu kısa biçim yalnızca fee bildirimi açıkken gider.",
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


def format_export_prompt() -> FormattedMessage:
    from notifications.telegram_keyboards import export_keyboard

    body = "\n".join([
        "Aralık seç veya komut yaz.",
        "Örnek: <code>/export 7d ethereum VARIANT</code>",
        "Özel aralık: <code>2026-10-01 2026-10-08</code>",
        "Bitiş anı dahil değildir. En fazla 366 gün.",
        "Tespit filtresi: transfer, approval, poisoning.",
    ])
    return FormattedMessage(_with_separator("📤 <b>CSV dışa aktarma</b>", body), reply_markup=export_keyboard())


def format_export_notice(text: str) -> FormattedMessage:
    return FormattedMessage(
        _with_separator("📤 <b>CSV dışa aktarma</b>", escape(text)),
        reply_markup=back_to_main_keyboard(),
    )


def format_weekly_summary(summary) -> FormattedMessage:
    from audit_log import CLASSIFICATION_LABELS
    from reporting.weekly import RISK_LEVELS, change_text

    def feature(value) -> str:
        return "kayıt yok" if value is None else str(value)

    classes = dict(summary.by_classification)
    class_line = " · ".join(
        f"{escape(CLASSIFICATION_LABELS.get(name, name))}: {classes.get(name, 0)}"
        for name in ("CANONICAL", "VARIANT", "KNOWN_FRAUD", "UNKNOWN", "ERROR")
    )
    if summary.by_network:
        network_line = " · ".join(f"{escape(name)}: {count}" for name, count in summary.by_network)
    else:
        network_line = "0"
    feedback = summary.feedback
    if summary.risk_levels is None:
        risk_line = "kayıt yok"
    else:
        icons = {"DÜŞÜK": "🟢", "ORTA": "🟡", "YÜKSEK": "🟠", "KRİTİK": "🔴"}
        levels = dict(summary.risk_levels)
        risk_line = " · ".join(f"{icons.get(name, '⚪')} {name}: {levels.get(name, 0)}" for name in RISK_LEVELS)
    body = "\n".join([
        f"📅 {escape(summary.start.strftime('%Y-%m-%d %H:%M'))} UTC – {escape(summary.end.strftime('%Y-%m-%d %H:%M'))} UTC",
        "ℹ️ Hafta pazartesi 00:00 UTC başlar. Bitiş anı dahil değildir.",
        f"🔁 Transfer: <b>{summary.transfers}</b>",
        f"🌐 {network_line}",
        f"🛡️ {class_line}",
        f"🚨 Şüpheli (varyant + bilinen sahte): <b>{summary.suspicious}</b>",
        f"🎭 Address poisoning: <b>{escape(feature(summary.poisoning))}</b>",
        f"✅ Approval: <b>{escape(feature(summary.approvals))}</b>",
        f"📈 Risk: {risk_line}",
        f"📨 Gönderilen bildirim: <b>{summary.notifications}</b>",
        f"👍 Doğru: {feedback.get('CORRECT', 0)} · 👎 Yanlış alarm: {feedback.get('FALSE_POSITIVE', 0)} · ❓ Emin değil: {feedback.get('UNSURE', 0)}",
        f"🏷️ Etiketli örnek: <b>{summary.labeled}</b>",
        f"📉 Yanlış alarm oranı: <b>{escape(summary.false_alarm)}</b>",
        f"📊 Önceki haftaya göre transfer: <b>{escape(change_text(summary.transfers, summary.previous_transfers))}</b>",
        "ℹ️ UNKNOWN sahte sayılmaz. Geri bildirimsiz tespit doğru veya yanlış sayılmaz.",
        "ℹ️ Oran, etiket yoksa N/A; 5 etiketli örnekten azsa hesaplanmaz.",
    ])
    return FormattedMessage(_with_separator("🗓️ <b>Haftalık özet</b>", body))


def format_setting_prompt(field: str, network: str) -> FormattedMessage:
    prompts = {
        "alias": "Takma adı gönder. Boşaltmak için - yaz. En fazla 32 karakter.",
        "amount": "Minimum token tutarını gönder. 0 tüm tutarları kabul eder. Örnek: 1.5",
        "quiet": "Sessiz saat aralığını UTC olarak gönder. Örnek: 22:00-08:00. Kapatmak için kapat yaz.",
    }
    return FormattedMessage(
        _with_separator(
            f"⚙️ <b>{escape(get_network_config(network).display_name)} ayarı</b>",
            f"{escape(prompts[field])}\n⏳ Bu istek 5 dakika geçerlidir.",
        ),
        reply_markup=cancel_keyboard(),
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
    from networks import get_network_config
    transfer_list = list(transfers)
    header = (
        f"🕘 <b>Son {len(transfer_list)} {'TRC-20' if transfer_list and transfer_list[0].network == 'tron' else 'ERC-20'} Transferi</b>\n"
        f"{SEPARATOR}\n"
        f"👛 Cüzdan: <code>{escape(wallet_address)}</code>\n"
        f"🌐 Ağ: {escape(get_network_config(transfer_list[0].network).display_name if transfer_list else 'Ethereum')}"
    )
    chunks: list[str] = []
    current = header
    for transfer in transfer_list:
        entry = "\n" + _history_entry(
            transfer,
            prices.get(transfer.token_address if transfer.network == "tron" else transfer.token_address.lower()),
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
    network: str = "ethereum",
) -> FormattedMessage:
    from networks import get_network_config
    token_standard = "TRC-20" if network == "tron" else "ERC-20"
    return FormattedMessage(
        _with_separator(
            "📄 <b>Transfer Geçmişi</b>",
            f"ℹ️ Bu cüzdan için {token_standard} transferi bulunamadı.\n👛 <code>{escape(get_network_config(network).normalize_address(wallet_address))}</code>",
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
            "Bir token kontratı veya işlem hash'i gönder. Ağ belirtilmezse adres biçimine göre ağ seçilir.\n\n"
            "Örnekler:\n<code>/check 0x0000000000000000000000000000000000000000</code>\n"
            "<code>/check tron TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t</code>\n"
            "<code>/check tron 0000000000000000000000000000000000000000000000000000000000000000</code>",
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


def format_check_loading() -> FormattedMessage:
    return FormattedMessage(
        _with_separator(
            "⏳ <b>Kontrol sürüyor</b>",
            "İşlem blokzincirde inceleniyor. Lütfen bekle.",
        ),
        reply_markup=back_to_main_keyboard(),
    )


def format_history_loading(address: str) -> FormattedMessage:
    return FormattedMessage(
        _with_separator(
            "⏳ <b>Geçmiş hazırlanıyor</b>",
            f"👛 Cüzdan: <code>{escape(address)}</code>\n"
            "Transferler blokzincirden alınıyor. Lütfen bekle.",
        ),
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
    symbol = display_token_text(info.symbol or "Bilinmiyor")
    name = display_token_text(info.name or "")
    rows = [
        f"{severity} <b>Token Kontrolü · {escape(label)}</b>",
        SEPARATOR,
        f"🌐 Ağ: {escape(get_network_config(result.network).display_name)}",
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


def _native_asset(network: str) -> tuple[str, str]:
    if network == "tron":
        return "TRX", "TRX transferi"
    return "ETH", "ETH transferi"


def format_tx_check(result, *, eth_usd_price: Decimal | None = None, address_names: dict[str, str] | None = None) -> FormattedMessage:
    from notifications.telegram_keyboards import url_button
    from networks import get_network_config

    if result.transaction is None:
        return format_check_error("not_found")
    config = get_network_config(result.network)
    classification = result.classification
    severity, label = CLASSIFICATION_STYLE.get(classification, CLASSIFICATION_STYLE["UNKNOWN"])
    transaction = result.transaction
    symbol, native_label = _native_asset(result.network)
    native_transfer = transaction.native_value > 0 and transaction.status == "success"
    if not result.transfers:
        label = native_label if native_transfer else "Token transferi yok"
    status = {"success": "✅ Başarılı", "failed": "❌ Başarısız", "pending": "⏳ Beklemede"}.get(transaction.status, "⏳ Beklemede")
    rows = [
        f"{severity} <b>İşlem Kontrolü · {escape(label)}</b>",
        SEPARATOR,
        f"🌐 Ağ: {escape(get_network_config(result.network).display_name)}",
        f"🔗 TXID: <code>{escape(result.tx_hash)}</code>",
        status,
    ]
    if result.network == "tron":
        rows.append("✅ Ağda onaylandı" if transaction.confirmed else "⏳ Ağ onayı bekleniyor")
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
    if not result.transfers and not native_transfer:
        standard = "TRC-20" if result.network == "tron" else "ERC-20"
        rows.append(f"ℹ️ Bu işlemde {standard} token transferi bulunamadı.")
    if transaction.native_value > 0:
        usd_value = format_usd(transaction.native_value, eth_usd_price) if result.network == "ethereum" else None
        if usd_value is not None:
            usd_suffix = f" ({escape(usd_value)})"
        elif result.network == "ethereum":
            usd_suffix = " (USD fiyatı alınamadı)"
        else:
            usd_suffix = ""
        if native_transfer:
            rows.extend([
                SEPARATOR,
                f"💠 <b>{escape(native_label)}</b>",
                f"🪙 Miktar: <b>{escape(format_token_amount(transaction.native_value))} <code>{escape(symbol)}</code>{usd_suffix}</b>",
                _address_line("📤 Gönderen", transaction.from_address, address_names),
            ])
            if transaction.to_address:
                rows.append(_address_line("📥 Alıcı", transaction.to_address, address_names))
        else:
            rows.extend([
                SEPARATOR,
                f"💠 İşlemdeki {escape(symbol)} değeri: <b>{escape(format_token_amount(transaction.native_value))} <code>{escape(symbol)}</code>{usd_suffix}</b>",
                f"⚠️ İşlem başarısız veya beklemede olduğu için {escape(symbol)} transferi kesinleşmedi.",
            ])
    visible = list(result.transfers[:5])
    for index, item in enumerate(visible, 1):
        transfer = item.transfer
        style, item_label = CLASSIFICATION_STYLE.get(item.analysis.classification, CLASSIFICATION_STYLE["UNKNOWN"])
        symbol = display_token_text(transfer.token_symbol)
        rows.extend([
            SEPARATOR,
            f"{style} <b>Token transferi {index} · {escape(item_label)}</b>",
            f"🪙 Miktar: <b>{escape(format_token_amount(transfer.amount))} <code>{escape(symbol)}</code></b>",
            f"🛡️ Sınıflandırma: <b>{escape(item_label)}</b>",
            _address_line("📤 Gönderen", transfer.sender, address_names),
            _address_line("📥 Alıcı", transfer.receiver, address_names),
            _address_line("📄 Token kontratı", transfer.token_address, address_names),
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
