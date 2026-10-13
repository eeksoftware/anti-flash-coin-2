"""Public Telegram profile fields for users the panel already knows.

The bot token stays in this process. File ids and download paths are not
returned. A missing profile stays missing; names are not invented.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor


logger = logging.getLogger(__name__)

_USER_ID = re.compile(r"^[0-9]{1,20}$")
_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,32}$")
_FILE_PATH = re.compile(r"^[A-Za-z0-9_./-]{1,180}$")
_MAX_PHOTO = 400_000
_PROFILE_TTL = 600
_MISS_TTL = 60
_PHOTO_TTL = 1800
_lock = threading.Lock()
_profiles: dict[str, tuple[float, dict, str]] = {}
_photos: dict[str, tuple[float, bytes, str]] = {}


def attach_profiles(payload: dict, *, token: str, call=None) -> dict:
    users = [user for user in payload.get("users") or [] if isinstance(user, dict)]
    if call is None and len(users) > 1 and token:
        with ThreadPoolExecutor(max_workers=min(4, len(users))) as pool:
            found = list(pool.map(lambda user: read_profile(str(user.get("user_id") or ""), token=token), users))
    else:
        found = [read_profile(str(user.get("user_id") or ""), token=token, call=call) for user in users]
    for user, profile in zip(users, found):
        user["profile"] = profile
    return payload


def read_profile(user_id: str, *, token: str, call=None) -> dict:
    if not _USER_ID.fullmatch(user_id):
        return _blank()
    if call is not None:
        public, _file_id = _from_chat(call, user_id)
        return public
    cached = _cached_profile(user_id)
    if cached is not None:
        return cached
    if not token:
        return _blank()
    public, file_id = _from_chat(lambda method, params: _telegram(token, method, params), user_id)
    _store_profile(user_id, public, file_id, hit=public["available"])
    return public


def read_photo(user_id: str, *, token: str, call=None, download=None) -> tuple[bytes, str] | None:
    if not _USER_ID.fullmatch(user_id) or (call is None and not token):
        return None
    cached = _cached_photo(user_id) if call is None else None
    if cached is not None:
        return cached
    profile = _cached_entry(user_id) if call is None else None
    file_id = profile[2] if profile else ""
    if not file_id:
        if call is None:
            read_profile(user_id, token=token)
            profile = _cached_entry(user_id)
            file_id = profile[2] if profile else ""
        else:
            _public, file_id = _from_chat(call, user_id)
    if not file_id:
        return None
    caller = call or (lambda method, params: _telegram(token, method, params))
    described = caller("getFile", {"file_id": file_id})
    if not isinstance(described, dict):
        return None
    path = str(described.get("file_path") or "")
    size = described.get("file_size")
    if not _FILE_PATH.fullmatch(path) or ".." in path:
        return None
    if isinstance(size, int) and size > _MAX_PHOTO:
        return None
    if download is None:
        body = _download(token, path)
    else:
        body = download(path)
    if not isinstance(body, bytes) or not body or len(body) > _MAX_PHOTO:
        return None
    kind = _image_type(body)
    if kind is None:
        return None
    if call is None:
        with _lock:
            _photos[user_id] = (time.monotonic() + _PHOTO_TTL, body, kind)
    return body, kind


def _from_chat(call, user_id: str) -> tuple[dict, str]:
    chat = call("getChat", {"chat_id": user_id})
    if not isinstance(chat, dict) or str(chat.get("type") or "") != "private":
        return _blank(), ""
    first = _text(chat.get("first_name"), 64)
    last = _text(chat.get("last_name"), 64)
    username = str(chat.get("username") or "")
    if not _USERNAME.fullmatch(username):
        username = ""
    name = " ".join(part for part in (first, last) if part)
    public = {
        "available": True,
        "name": name,
        "first_name": first,
        "last_name": last,
        "username": username,
        "bio": _text(chat.get("bio"), 400),
        "has_photo": False,
    }
    file_id = _file_id(chat.get("photo"))
    if not file_id:
        file_id = _profile_photo_id(call("getUserProfilePhotos", {"user_id": user_id, "limit": 1}))
    public["has_photo"] = bool(file_id)
    return public, file_id


def _file_id(photo) -> str:
    if not isinstance(photo, dict):
        return ""
    value = str(photo.get("big_file_id") or photo.get("small_file_id") or "")
    return value if 8 <= len(value) <= 200 else ""


def _profile_photo_id(result) -> str:
    if not isinstance(result, dict):
        return ""
    photos = result.get("photos")
    if not isinstance(photos, list) or not photos or not isinstance(photos[0], list) or not photos[0]:
        return ""
    largest = photos[0][-1]
    if not isinstance(largest, dict):
        return ""
    return _file_id({"big_file_id": largest.get("file_id")})


def _telegram(token: str, method: str, params: dict):
    if method not in {"getChat", "getUserProfilePhotos", "getFile"}:
        return None
    url = "https://api.telegram.org/bot" + token + "/" + method
    body = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None}).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=4, context=_ssl_context()) as response:
            if not response.geturl().startswith("https://api.telegram.org/"):
                return None
            payload = json.loads(response.read(65536).decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        logger.info("Telegram profili okunamadı.")
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    return payload.get("result")


def _download(token: str, file_path: str) -> bytes | None:
    url = "https://api.telegram.org/file/bot" + token + "/" + file_path
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=4, context=_ssl_context()) as response:
            if not response.geturl().startswith("https://api.telegram.org/file/"):
                return None
            return response.read(_MAX_PHOTO + 1)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        logger.info("Telegram profil fotoğrafı okunamadı.")
        return None


def _ssl_context():
    cafile = os.environ.get("SSL_CERT_FILE", "/etc/ssl/cert.pem")
    if os.path.isfile(cafile):
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def _image_type(body: bytes) -> str | None:
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(body) >= 12 and body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "image/webp"
    return None


def _text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(char for char in value if char == "\n" or ord(char) >= 32)
    return cleaned.strip()[:limit]


def _blank() -> dict:
    return {
        "available": False,
        "name": "",
        "first_name": "",
        "last_name": "",
        "username": "",
        "bio": "",
        "has_photo": False,
    }


def _store_profile(user_id: str, public: dict, file_id: str, *, hit: bool) -> None:
    ttl = _PROFILE_TTL if hit else _MISS_TTL
    with _lock:
        _profiles[user_id] = (time.monotonic() + ttl, dict(public), file_id)


def _cached_entry(user_id: str):
    with _lock:
        found = _profiles.get(user_id)
        if found is None or found[0] <= time.monotonic():
            return None
        return found


def _cached_profile(user_id: str) -> dict | None:
    found = _cached_entry(user_id)
    return dict(found[1]) if found else None


def _cached_photo(user_id: str):
    with _lock:
        found = _photos.get(user_id)
        if found is None or found[0] <= time.monotonic():
            return None
        return found[1], found[2]
