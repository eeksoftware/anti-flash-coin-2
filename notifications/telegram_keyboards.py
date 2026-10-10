from typing import Any
from urllib.parse import quote

from networks import NETWORKS as NETWORK_CONFIGS, get_network_config


NETWORKS = {name: f"🔷 {config.display_name}" for name, config in NETWORK_CONFIGS.items()}

MENU_MAIN = "menu:main"
MENU_WALLETS = "menu:wallets"
MENU_ADD = "menu:add"
MENU_HISTORY = "menu:history"
MENU_STATUS = "menu:status"
MENU_HELP = "menu:help"
MENU_ABOUT = "menu:about"
MENU_CHECK = "menu:check"
WALLET_ADD_PREFIX = "w:add:"
WALLET_CANCEL = "w:cancel"
WALLET_HISTORY_PREFIX = "w:h:"
WALLET_HISTORY_REFRESH_PREFIX = "w:hr:"
WALLET_REMOVE_PREFIX = "w:rm:"
WALLET_REMOVE_CONFIRM_PREFIX = "w:rmok:"
WALLET_REMOVE_CANCEL_PREFIX = "w:rmno:"
WALLET_LIST_PREFIX = "w:list:"


InlineKeyboard = dict[str, list[list[dict[str, Any]]]]


def callback_button(text: str, callback_data: str) -> dict[str, str]:
    return {"text": text, "callback_data": callback_data}


def url_button(text: str, url: str) -> dict[str, str]:
    return {"text": text, "url": url}


def inline_keyboard(rows: list[list[dict[str, Any]]]) -> InlineKeyboard:
    return {"inline_keyboard": rows}


def main_menu_keyboard() -> InlineKeyboard:
    return inline_keyboard([
        [callback_button("➕ Cüzdan Ekle", MENU_ADD), callback_button("📋 Cüzdanlarım", MENU_WALLETS)],
        [callback_button("🕘 Geçmiş", MENU_HISTORY), callback_button("📊 Durum", MENU_STATUS)],
        [callback_button("🔍 Kontrol Et", MENU_CHECK)],
        [callback_button("❓ Yardım", MENU_HELP), callback_button("ℹ️ Hakkında", MENU_ABOUT)],
    ])


def private_chat_keyboard(bot_username: str | None) -> InlineKeyboard | None:
    if not bot_username:
        return None
    return inline_keyboard([[
        url_button("🤖 Botu özel mesajda aç", f"https://t.me/{quote(bot_username)}?start=menu")
    ]])


def add_network_keyboard() -> InlineKeyboard:
    rows = [
        [callback_button(label, f"{WALLET_ADD_PREFIX}{network}")]
        for network, label in NETWORKS.items()
    ]
    rows.append([callback_button("❌ İptal", WALLET_CANCEL)])
    return inline_keyboard(rows)


def cancel_keyboard() -> InlineKeyboard:
    return inline_keyboard([[callback_button("❌ İptal", WALLET_CANCEL)]])


def check_result_keyboard(
    network: str,
    value: str,
    *,
    token: bool = False,
    suspicious: bool = False,
    wallet: bool = False,
) -> InlineKeyboard:
    config = get_network_config(network)
    rows = [[url_button("🔍 İşlemi Aç", config.transaction_url(quote(value, safe="")))]] if not token else []
    if wallet:
        rows.append([url_button("👛 Adresi Aç", config.address_url(quote(value, safe="")))])
    elif token or suspicious:
        rows.append([url_button("📄 Kontratı Aç", config.token_url(quote(value, safe="")))])
    rows.append([callback_button("⬅️ Ana Menü", MENU_MAIN)])
    return inline_keyboard(rows)


def wallets_keyboard(wallets: list[Any], page: int = 0, page_size: int = 8) -> InlineKeyboard:
    start = max(0, page) * page_size
    current = wallets[start : start + page_size]
    rows: list[list[dict[str, Any]]] = []
    for index, wallet in enumerate(current, start=start + 1):
        address = wallet.wallet_address.lower()
        rows.append([
            callback_button(f"🕘 Geçmiş #{index}", f"{WALLET_HISTORY_PREFIX}{address}"),
            callback_button(f"🗑️ Kaldır #{index}", f"{WALLET_REMOVE_PREFIX}{address}"),
        ])

    if start > 0 or start + page_size < len(wallets):
        page_row: list[dict[str, Any]] = []
        if start > 0:
            page_row.append(callback_button("◀️ Önceki", f"{WALLET_LIST_PREFIX}{page - 1}"))
        if start + page_size < len(wallets):
            page_row.append(callback_button("Sonraki ▶️", f"{WALLET_LIST_PREFIX}{page + 1}"))
        rows.append(page_row)
    rows.extend([
        [callback_button("➕ Cüzdan Ekle", MENU_ADD)],
        [callback_button("⬅️ Ana Menü", MENU_MAIN)],
    ])
    return inline_keyboard(rows)


def history_wallets_keyboard(wallets: list[Any]) -> InlineKeyboard:
    rows = [
        [callback_button(f"🕘 Geçmiş #{index}", f"{WALLET_HISTORY_PREFIX}{wallet.wallet_address.lower()}")]
        for index, wallet in enumerate(wallets, 1)
    ]
    rows.append([callback_button("⬅️ Ana Menü", MENU_MAIN)])
    return inline_keyboard(rows)


def history_result_keyboard(address: str) -> InlineKeyboard:
    return inline_keyboard([
        [callback_button("🔄 Yenile", f"{WALLET_HISTORY_REFRESH_PREFIX}{address.lower()}")],
        [callback_button("⬅️ Geri", MENU_HISTORY)],
    ])


def status_keyboard() -> InlineKeyboard:
    return inline_keyboard([
        [callback_button("🔄 Yenile", MENU_STATUS)],
        [callback_button("⬅️ Ana Menü", MENU_MAIN)],
    ])


def back_to_main_keyboard() -> InlineKeyboard:
    return inline_keyboard([[callback_button("⬅️ Ana Menü", MENU_MAIN)]])


def wallet_added_keyboard() -> InlineKeyboard:
    return inline_keyboard([[
        callback_button("📋 Cüzdanlarım", MENU_WALLETS),
        callback_button("⬅️ Ana Menü", MENU_MAIN),
    ]])


def remove_confirmation_keyboard(address: str) -> InlineKeyboard:
    return inline_keyboard([[
        callback_button("✅ Evet, kaldır", f"{WALLET_REMOVE_CONFIRM_PREFIX}{address.lower()}"),
        callback_button("❌ Vazgeç", f"{WALLET_REMOVE_CANCEL_PREFIX}{address.lower()}"),
    ]])


def transfer_alert_keyboard(network: str, tx_hash: str, token_address: str, wallet_address: str) -> InlineKeyboard:
    try:
        config = get_network_config(network)
    except ValueError:
        return inline_keyboard([])
    return inline_keyboard([[
        url_button("🔍 İşlemi Aç", config.transaction_url(quote(tx_hash, safe=""))),
        url_button("📄 Kontratı Aç", config.token_url(quote(token_address, safe=""))),
        url_button("👛 Cüzdan", config.address_url(quote(wallet_address, safe=""))),
    ]])
