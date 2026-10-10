"""Central authorization helper for private admin-only commands."""
import hashlib
import logging

logger = logging.getLogger(__name__)


def parse_admin_ids(raw: str | None) -> frozenset[str]:
    values = set()
    for value in (raw or "").split(","):
        value = value.strip()
        if value.isdigit(): values.add(value)
    return frozenset(values)


def admin_command_allowed(message: dict, admin_ids) -> bool:
    sender = message.get("from")
    chat = message.get("chat")
    user_id = str(sender.get("id", "")) if isinstance(sender, dict) else ""
    private = isinstance(chat, dict) and chat.get("type") == "private" and str(chat.get("id", "")) == user_id
    if private and user_id in admin_ids:
        return True
    digest = hashlib.sha256(user_id.encode("utf-8", "replace")).hexdigest()[:12]
    logger.warning("Yetkisiz admin komutu yok sayıldı | user_hash=%s", digest)
    return False
