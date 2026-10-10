import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    etherscan_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str

    monitored_wallet: str
    network: str

    poll_interval: float
    max_poll_interval: float
    log_dir: str = "./logs"
    log_level: str = "INFO"
    log_retention_days: int = 90


def load_settings() -> Settings:
    return Settings(
        etherscan_api_key=os.getenv("ETHERSCAN_API_KEY", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        monitored_wallet=os.getenv(
            "MONITORED_WALLET",
            "0x285a3484FBf2598c7133A7F81D9D268BDC80EB42",
        ).lower(),

        network=os.getenv("NETWORK", "ethereum").lower(),

        poll_interval=float(os.getenv("POLL_INTERVAL", "2")),
        max_poll_interval=float(os.getenv("MAX_POLL_INTERVAL", "30")),
        log_dir=os.getenv("LOG_DIR", "./logs"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        log_retention_days=int(os.getenv("LOG_RETENTION_DAYS", "90")),
    )


settings = load_settings()
