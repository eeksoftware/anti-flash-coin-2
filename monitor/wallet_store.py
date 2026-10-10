import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from networks import get_network_config
from config import settings


logger = logging.getLogger(__name__)


def _normalize_subscription(network: str, address: str) -> tuple[str, str]:
    config = get_network_config(network)
    return config.name, config.normalize_address(address)


@dataclass(frozen=True)
class WalletSubscription:
    user_id: str | None
    network: str
    wallet_address: str


class WalletStateStore:
    """Persist legacy configured watches and per-Telegram-user subscriptions."""

    def __init__(self, path: Path | None = None) -> None:
        data_dir = Path(settings.data_dir)
        if not data_dir.is_absolute():
            data_dir = Path(__file__).resolve().parents[1] / data_dir
        self.path = path or data_dir / "runtime_state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path = Path(__file__).resolve().parents[1] / "runtime_state.json"
        if path is None and not self.path.exists() and legacy_path.exists():
            try:
                import shutil
                shutil.copy2(legacy_path, self.path)
                os.chmod(self.path, 0o600)
                logger.info("Cüzdan verisi eski konumdan DATA_DIR konumuna kopyalandı.")
            except OSError:
                # Keep reading the original file if a safe copy cannot be made.
                logger.warning("Cüzdan verisi kopyalanamadı; eski dosyadan okunmaya devam ediliyor.")
                self.path = legacy_path
        self._legacy: list[dict[str, str]] = []
        self._users: dict[str, list[dict[str, str]]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cüzdan kayıtları okunamadı: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("Cüzdan kayıtları JSON nesnesi biçiminde olmalı.")

        # Migrate the earlier singleton format without losing the configured watch.
        if isinstance(data.get("wallet_address"), str):
            network, wallet_address = _normalize_subscription(
                str(data.get("network", "ethereum")), data["wallet_address"]
            )
            self._legacy = [{
                "network": network,
                "wallet_address": wallet_address,
            }]
            self._write()
            return

        legacy = data.get("legacy", [])
        users = data.get("users", {})
        if isinstance(legacy, list):
            self._legacy = [self._normalize_item(item) for item in legacy if self._valid_item(item)]
        if isinstance(users, dict):
            self._users = {
                str(user_id): [self._normalize_item(item) for item in items if self._valid_item(item)]
                for user_id, items in users.items()
                if isinstance(items, list)
            }

    @staticmethod
    def _valid_item(item: object) -> bool:
        if not isinstance(item, dict) or not isinstance(item.get("network"), str) or not isinstance(item.get("wallet_address"), str):
            return False
        try:
            return get_network_config(item["network"]).is_valid_address(item["wallet_address"])
        except ValueError:
            return False

    @staticmethod
    def _normalize_item(item: dict[str, str]) -> dict[str, str]:
        network, address = _normalize_subscription(item["network"], item["wallet_address"])
        return {"network": network, "wallet_address": address}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"legacy": self._legacy, "users": self._users}
        fd, temporary_path = tempfile.mkstemp(prefix="wallet-state-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(payload, output, indent=2)
                output.write("\n")
            try: os.chmod(temporary_path, 0o600)
            except OSError: pass
            os.replace(temporary_path, self.path)
            try: os.chmod(self.path, 0o600)
            except OSError: pass
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def ensure_legacy_wallet(self, network: str, wallet_address: str) -> None:
        config = get_network_config(network)
        if not self._legacy and config.is_valid_address(wallet_address):
            self._legacy.append({"network": config.name, "wallet_address": config.normalize_address(wallet_address)})
            self._write()

    def all_subscriptions(self) -> list[WalletSubscription]:
        return [
            WalletSubscription(None, item["network"], item["wallet_address"])
            for item in self._legacy
        ] + [
            WalletSubscription(user_id, item["network"], item["wallet_address"])
            for user_id, items in self._users.items()
            for item in items
        ]

    def for_user(self, user_id: str) -> list[WalletSubscription]:
        return [
            WalletSubscription(user_id, item["network"], item["wallet_address"])
            for item in self._users.get(str(user_id), [])
        ]

    def add_wallet(self, user_id: str, network: str, wallet_address: str) -> bool:
        config = get_network_config(network)
        if not config.is_valid_address(wallet_address):
            raise ValueError("Cüzdan adresi geçersiz.")
        user_id = str(user_id)
        network = config.name
        wallet_address = config.normalize_address(wallet_address)
        items = self._users.setdefault(user_id, [])
        if any(i["network"] == network and i["wallet_address"] == wallet_address for i in items):
            return False
        items.append({"network": network, "wallet_address": wallet_address})
        self._write()
        return True

    def remove_wallet(self, user_id: str, network: str, wallet_address: str) -> bool:
        config = get_network_config(network)
        user_id = str(user_id)
        network = config.name
        wallet_address = config.normalize_address(wallet_address)
        items = self._users.get(user_id, [])
        kept = [i for i in items if not (
            i["network"] == network
            and i["wallet_address"] == wallet_address
        )]
        if len(kept) == len(items):
            return False
        if kept:
            self._users[user_id] = kept
        else:
            self._users.pop(user_id, None)
        self._write()
        return True
