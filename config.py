import os
from dataclasses import dataclass, field

from dotenv import load_dotenv
from infra.key_pool import Secret


load_dotenv()
from networks import get_enabled_networks, get_network_config


@dataclass(frozen=True)
class Settings:
    etherscan_api_key: Secret = field(repr=False)
    etherscan_api_keys: tuple[Secret, ...] = field(repr=False)
    etherscan_rate_per_key: float
    etherscan_burst: int
    etherscan_daily_limit: int
    etherscan_daily_soft_ratio: float
    daily_reset_utc_hour: int
    api_acquire_timeout_interactive: float
    api_acquire_timeout_background: float
    cache_max_entries: int
    monitor_min_interval: float
    monitor_max_interval: float
    telegram_bot_token: str
    telegram_chat_id: str
    admin_ids: tuple[str, ...]
    data_dir: str
    heartbeat_url: str
    trongrid_api_keys: tuple[Secret, ...]
    trongrid_base_url: str
    trongrid_rate_per_key: float
    trongrid_burst: int
    trongrid_daily_limit: int
    trongrid_daily_soft_ratio: float
    tron_only_confirmed: bool
    enabled_networks: tuple[str, ...]

    monitored_wallet: str
    network: str

    poll_interval: float
    max_poll_interval: float
    log_dir: str = "./logs"
    log_level: str = "INFO"
    log_retention_days: int = 90


def load_settings() -> Settings:
    network_name = os.getenv("NETWORK", "ethereum").lower()
    monitored_wallet = os.getenv(
        "MONITORED_WALLET",
        "0x285a3484FBf2598c7133A7F81D9D268BDC80EB42",
    )
    try:
        monitored_wallet = get_network_config(network_name).normalize_address(monitored_wallet)
    except ValueError:
        monitored_wallet = monitored_wallet.lower()
    configured_keys = os.getenv("ETHERSCAN_API_KEYS", "")
    raw_keys = configured_keys.split(",") if configured_keys.strip() else [os.getenv("ETHERSCAN_API_KEY", "")]
    keys: list[str] = []
    seen: set[str] = set()
    for raw_key in raw_keys:
        key = raw_key.strip()
        if key and key not in seen:
            if key in keys:
                import logging
                logging.getLogger(__name__).warning("Yinelenen Etherscan API anahtarı atlandı.")
            keys.append(key)
            seen.add(key)
        elif key:
            import logging
            logging.getLogger(__name__).warning("Yinelenen Etherscan API anahtarı atlandı.")
    return Settings(
        etherscan_api_key=Secret(keys[0] if keys else ""),
        etherscan_api_keys=tuple(Secret(key) for key in keys),
        etherscan_rate_per_key=float(os.getenv("ETHERSCAN_RATE_PER_KEY", "4")),
        etherscan_burst=int(os.getenv("ETHERSCAN_BURST", "2")),
        etherscan_daily_limit=int(os.getenv("ETHERSCAN_DAILY_LIMIT", "100000")),
        etherscan_daily_soft_ratio=float(os.getenv("ETHERSCAN_DAILY_SOFT_RATIO", "0.95")),
        daily_reset_utc_hour=int(os.getenv("DAILY_RESET_UTC_HOUR", "0")),
        api_acquire_timeout_interactive=float(os.getenv("API_ACQUIRE_TIMEOUT_INTERACTIVE", "8")),
        api_acquire_timeout_background=float(os.getenv("API_ACQUIRE_TIMEOUT_BACKGROUND", "60")),
        cache_max_entries=int(os.getenv("CACHE_MAX_ENTRIES", "10000")),
        monitor_min_interval=float(os.getenv("MONITOR_MIN_INTERVAL", os.getenv("POLL_INTERVAL", "2"))),
        monitor_max_interval=float(os.getenv("MONITOR_MAX_INTERVAL", os.getenv("MAX_POLL_INTERVAL", "30"))),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        admin_ids=tuple(dict.fromkeys(value.strip() for value in os.getenv("ADMIN_IDS", "").split(",") if value.strip().isdigit())),
        data_dir=os.getenv("DATA_DIR", "./data"),
        heartbeat_url=os.getenv("HEARTBEAT_URL", "").strip(),
        trongrid_api_keys=tuple(Secret(value.strip()) for value in dict.fromkeys(os.getenv("TRONGRID_API_KEYS", "").split(",")) if value.strip()),
        trongrid_base_url=os.getenv("TRONGRID_BASE_URL", "https://api.trongrid.io").rstrip("/"),
        trongrid_rate_per_key=float(os.getenv("TRONGRID_RATE_PER_KEY", "10")),
        trongrid_burst=int(os.getenv("TRONGRID_BURST", "3")),
        trongrid_daily_limit=int(os.getenv("TRONGRID_DAILY_LIMIT", "100000")),
        trongrid_daily_soft_ratio=float(os.getenv("TRONGRID_DAILY_SOFT_RATIO", "0.95")),
        tron_only_confirmed=os.getenv("TRON_ONLY_CONFIRMED", "true").strip().lower() in {"1", "true", "yes", "on"},
        enabled_networks=tuple(get_enabled_networks().keys()),
        monitored_wallet=monitored_wallet,

        network=network_name,

        poll_interval=float(os.getenv("POLL_INTERVAL", "2")),
        max_poll_interval=float(os.getenv("MAX_POLL_INTERVAL", "30")),
        log_dir=os.getenv("LOG_DIR", "./logs"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        log_retention_days=int(os.getenv("LOG_RETENTION_DAYS", "90")),
    )


settings = load_settings()
