import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


WALLET_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


@dataclass(frozen=True)
class WalletSubscription:
    user_id: str | None
    network: str
    wallet_address: str


class WalletStateStore:
    """Persist legacy configured watches and per-Telegram-user subscriptions."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(__file__).resolve().parents[1] / "runtime_state.json"
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
            self._legacy = [{
                "network": str(data.get("network", "ethereum")).lower(),
                "wallet_address": data["wallet_address"].lower(),
            }]
            self._write()
            return

        legacy = data.get("legacy", [])
        users = data.get("users", {})
        if isinstance(legacy, list):
            self._legacy = [item for item in legacy if self._valid_item(item)]
        if isinstance(users, dict):
            self._users = {
                str(user_id): [item for item in items if self._valid_item(item)]
                for user_id, items in users.items()
                if isinstance(items, list)
            }

    @staticmethod
    def _valid_item(item: object) -> bool:
        return (
            isinstance(item, dict)
            and isinstance(item.get("network"), str)
            and isinstance(item.get("wallet_address"), str)
            and WALLET_ADDRESS_RE.fullmatch(item["wallet_address"]) is not None
        )

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"legacy": self._legacy, "users": self._users}
        fd, temporary_path = tempfile.mkstemp(prefix="wallet-state-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(payload, output, indent=2)
                output.write("\n")
            os.replace(temporary_path, self.path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def ensure_legacy_wallet(self, network: str, wallet_address: str) -> None:
        if not self._legacy and WALLET_ADDRESS_RE.fullmatch(wallet_address):
            self._legacy.append({"network": network.lower(), "wallet_address": wallet_address.lower()})
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
        if WALLET_ADDRESS_RE.fullmatch(wallet_address) is None:
            raise ValueError("Cüzdan adresi geçersiz.")
        user_id = str(user_id)
        network = network.lower()
        wallet_address = wallet_address.lower()
        items = self._users.setdefault(user_id, [])
        if any(i["network"] == network and i["wallet_address"] == wallet_address for i in items):
            return False
        items.append({"network": network, "wallet_address": wallet_address})
        self._write()
        return True

    def remove_wallet(self, user_id: str, network: str, wallet_address: str) -> bool:
        user_id = str(user_id)
        items = self._users.get(user_id, [])
        kept = [i for i in items if not (
            i["network"] == network.lower()
            and i["wallet_address"] == wallet_address.lower()
        )]
        if len(kept) == len(items):
            return False
        if kept:
            self._users[user_id] = kept
        else:
            self._users.pop(user_id, None)
        self._write()
        return True
