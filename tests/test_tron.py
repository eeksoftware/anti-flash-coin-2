from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

import providers.tron as tron_module
from analyzer.token_analyzer import TokenAnalyzer
from blockchain.base import BlockchainClientError
from infra.cache import AsyncTTLCache
from infra.key_pool import Lease, Outcome, Priority, RateLimitedPool, Secret
from infra.metrics import APIMetrics
from models.transaction import TokenTransfer
from monitor.wallet_store import WalletStateStore, WalletSubscription
from networks import get_network_config
from notifications.telegram_formatter import format_transfer_alert
from notifications.telegram_keyboards import history_result_keyboard, wallets_keyboard
from providers.base import ChainProvider, ContractInfo
from providers.tron import TronGridError, TronGridRateLimitError, TronProvider
from services.check_input import parse_check_args


TRON = get_network_config("tron")
USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_HEX = "41a614f803b6fd780986a42c78ec9c7f77e6ded13c"
WALLET = "TJmmqjb1DK9TTZbQXzRQ2AuA94z4gKAPFh"
EVM_USDT = get_network_config("ethereum").official_usdt_contract
TXID = "a" * 64


def make_settings(tmp_path, keys=None, **overrides):
    values = {
        "trongrid_api_keys": tuple(Secret(key) for key in (keys or [])),
        "trongrid_base_url": "https://api.trongrid.io",
        "trongrid_rate_per_key": 10,
        "trongrid_burst": 3,
        "trongrid_daily_limit": 100000,
        "trongrid_daily_soft_ratio": .95,
        "tron_only_confirmed": True,
        "cache_max_entries": 100,
        "data_dir": str(tmp_path / "data"),
        "log_dir": str(tmp_path / "logs"),
        "api_acquire_timeout_background": .1,
        "api_acquire_timeout_interactive": .1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeResponse:
    def __init__(self, status, payload):
        self.status, self.payload = status, payload

    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None
    async def json(self, content_type=None): return self.payload


class FakeSession:
    closed = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse(*self.responses.pop(0))

    async def close(self): self.closed = True


class FakePool:
    def __init__(self):
        self.key = SimpleNamespace(label="tron-key-1", secret=Secret("sensitive-test-key"), cooldown_until=0)
        self._keys = [self.key]
        self.outcomes = []
        self._daily_limit = 95000
    async def acquire(self, **kwargs): return Lease(self.key.label, self.key.secret)
    def report(self, lease, outcome):
        self.outcomes.append(outcome)
        if outcome is Outcome.RATE_LIMITED: self.key.cooldown_until = 30
        if outcome is Outcome.INVALID_KEY: self.key.disabled = True
    def flush(self): pass


def bare_provider(session=None):
    provider = object.__new__(TronProvider)
    provider.base_url = "https://api.trongrid.io"
    provider.metrics = APIMetrics()
    provider.pool = FakePool()
    provider.cache = AsyncTTLCache()
    provider._session = session
    provider._owns_session = False
    provider._health_state = None
    provider.health_state = None
    provider._fingerprints = {}
    provider._fingerprint_filters = {}
    provider._last_timestamp = {}
    provider._contract_cache = {}
    provider._metrics_task = None
    return provider


def history_row(**overrides):
    row = {
        "transaction_id": TXID,
        "block_timestamp": 1_700_000_000_000,
        "block": 60_000_000,
        "event_index": 7,
        "from": WALLET,
        "to": "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE",
        "value": "12345678",
        "confirmed": True,
        "token_info": {"address": USDT, "symbol": "<b>USDT</b>", "name": "Tether", "decimals": 6},
    }
    row.update(overrides)
    return row


def test_tron_base58check_conversion_validates_and_preserves_case():
    assert TRON.address_to_hex(USDT) == USDT_HEX
    assert TRON.address_from_hex(USDT_HEX) == USDT
    assert TRON.is_valid_address(USDT)
    assert not TRON.is_valid_address(USDT[:-1] + "1")
    assert not TRON.is_valid_address(EVM_USDT)
    assert TRON.normalize_address(USDT) == USDT
    assert TRON.address_from_hex("a614f803b6fd780986a42c78ec9c7f77e6ded13c") == USDT


def test_check_input_auto_routes_address_hashes_and_keeps_evm_compatibility():
    assert parse_check_args([USDT]).network == "tron"
    assert parse_check_args([TXID]).network == "tron"
    assert parse_check_args(["0x" + TXID]).network == "ethereum"
    assert parse_check_args([EVM_USDT]).network == "ethereum"
    assert parse_check_args(["tron", "0x" + TXID]).value == TXID
    assert parse_check_args(["ethereum", EVM_USDT.upper().replace("0X", "0x")]).value == EVM_USDT.lower()


def test_existing_token_analyzer_reuses_network_address_registry_without_symbol_matching():
    analyzer = TokenAnalyzer()
    assert analyzer.analyze_token("tron", USDT, "fake").classification == "CANONICAL"
    assert analyzer.analyze_token("tron", EVM_USDT, "USDT").classification == "UNKNOWN"
    assert analyzer.analyze_token("tron", USDT.lower(), "USDT").classification == "UNKNOWN"


def test_tron_wallet_store_and_callbacks_keep_base58_spelling(tmp_path, monkeypatch):
    import monitor.wallet_store as wallet_store_module
    monkeypatch.setattr(wallet_store_module, "settings", make_settings(tmp_path))
    store = WalletStateStore(tmp_path / "wallets.json")
    assert store.add_wallet("123", "tron", USDT)
    assert not store.add_wallet("123", "tron", USDT)
    assert store.for_user("123")[0].wallet_address == USDT
    menu = wallets_keyboard(store.for_user("123"))
    for row in menu["inline_keyboard"]:
        for button in row:
            callback = button.get("callback_data", "")
            if callback.startswith(("w:h:", "w:rm:")):
                assert callback.endswith("tron:" + USDT)
                assert len(callback.encode()) <= 64
    refresh = history_result_keyboard(USDT, "tron")["inline_keyboard"][0][0]["callback_data"]
    assert refresh.endswith("tron:" + USDT)
    from notifications.telegram import TelegramBotController
    assert TelegramBotController._decode_wallet_callback("tron:" + USDT, store.for_user("123")) == ("tron", USDT)
    assert asyncio.run(_remove_wallet(store))


async def _remove_wallet(store):
    return store.remove_wallet("123", "tron", USDT)


def test_tron_history_row_parses_amount_direction_and_confirmed():
    transfer = TronProvider._transfer_from_history_row(history_row(), WALLET)
    assert transfer is not None
    assert transfer.amount == Decimal("12.345678")
    assert transfer.decimals == 6
    assert transfer.direction == "SEND"
    assert transfer.network == "tron" and transfer.confirmed is True
    assert transfer.token_address == USDT and transfer.token_symbol == "<b>USDT</b>"
    assert transfer.timestamp == datetime.fromtimestamp(1_700_000_000, timezone.utc)
    assert TronProvider._transfer_from_history_row(history_row(token_info={"address": USDT, "decimals": 999}), WALLET).decimals == 6
    assert TronProvider._transfer_from_history_row(history_row(token_info={"address": USDT, "decimals": 0}), WALLET).amount == Decimal("12345678")


def test_tron_provider_protocol_and_history_pagination_keeps_filters(monkeypatch):
    provider = bare_provider()
    responses = [
        {"data": [history_row()], "meta": {"fingerprint": "next-page"}},
        {"data": [history_row(transaction_id="b" * 64)], "meta": {}},
    ]
    calls = []
    async def request(method, path, *, params=None, **kwargs):
        calls.append((method, path, params))
        return responses.pop(0)
    provider._request = request
    assert isinstance(provider, ChainProvider)
    first = asyncio.run(provider.get_token_transfer_page(WALLET, 1, 100))
    second = asyncio.run(provider.get_token_transfer_page(WALLET, 2, 100))
    assert first[0].event_key == second[0].event_key == "log:7"
    assert calls[1][2]["fingerprint"] == "next-page"
    assert calls[1][2]["only_confirmed"] is True
    assert calls[1][2]["order_by"] == "block_timestamp,desc"


def test_trongrid_key_is_header_only_and_never_appears_in_errors_or_logs(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(tron_module, "settings", make_settings(tmp_path, ["sensitive-test-key"]))
    session = FakeSession([(200, {"data": []})])
    provider = TronProvider(session=session)
    provider.pool = FakePool()
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(provider._request("GET", "/v1/accounts/Tone/transactions/trc20", params={"limit": 1})) == {"data": []}
    method, url, kwargs = session.calls[0]
    assert method == "GET" and "api.trongrid.io" in url and "sensitive-test-key" not in url
    assert kwargs["headers"] == {"TRON-PRO-API-KEY": "sensitive-test-key"}
    assert kwargs["params"]["limit"] == 1
    assert "sensitive-test-key" not in caplog.text
    asyncio.run(provider.close())


def test_trongrid_bool_query_is_sent_as_text(tmp_path, monkeypatch):
    monkeypatch.setattr(tron_module, "settings", make_settings(tmp_path, ["key"]))
    session = FakeSession([(200, {"data": []})])
    provider = TronProvider(session=session)
    provider.pool = FakePool()
    asyncio.run(provider._request(
        "GET",
        "/v1/accounts/TXtest/transactions/trc20",
        params={"limit": 20, "only_confirmed": True},
    ))
    assert session.calls[0][2]["params"]["only_confirmed"] == "true"
    assert session.calls[0][2]["params"]["limit"] == 20
    asyncio.run(provider.close())


def test_trongrid_429_maps_rate_limit_and_pool_policy_sets_30s_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(tron_module, "settings", make_settings(tmp_path, ["key"]))
    session = FakeSession([(429, {"Error": "frequency limit"})] * 3)
    provider = TronProvider(session=session)
    provider.pool = FakePool()
    with pytest.raises(TronGridRateLimitError):
        asyncio.run(provider._request("GET", "/limited"))
    assert all(outcome is Outcome.RATE_LIMITED for outcome in provider.pool.outcomes)
    assert provider.pool.key.cooldown_until >= 30
    asyncio.run(provider.close())

    now = [10.0]
    pool = RateLimitedPool([Secret("x")], {"rate": 10, "burst": 3, "rate_limit_cooldown": 30},
        clock=lambda: now[0], jitter=lambda: 0)
    pool.report(Lease("key-1", pool._keys[0].secret), Outcome.RATE_LIMITED)
    assert pool._keys[0].cooldown_until - now[0] >= 30


def test_trongrid_401_disables_key_without_exposing_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(tron_module, "settings", make_settings(tmp_path, ["sensitive-key"]))
    session = FakeSession([(401, {"Error": "Invalid API Key"})] * 3)
    provider = TronProvider(session=session)
    provider.pool = FakePool()
    with pytest.raises(TronGridError) as error:
        asyncio.run(provider._request("GET", "/limited"))
    assert provider.pool.key.disabled is True
    assert "sensitive-key" not in str(error.value)
    asyncio.run(provider.close())


@pytest.mark.parametrize(("info", "solid", "expected"), [
    ({"blockNumber": 5, "blockTimeStamp": 1000, "receipt": {"result": "SUCCESS"}}, {"id": TXID}, "success"),
    ({"blockNumber": 5, "blockTimeStamp": 1000, "receipt": {"result": "FAILED"}}, {"id": TXID}, "failed"),
    ({"blockNumber": 0}, {}, "pending"),
])
def test_tron_transaction_status_and_confirmation(info, solid, expected):
    provider = bare_provider()
    body = {"txID": TXID, "ret": [{"contractRet": "SUCCESS"}], "raw_data": {"contract": []}}
    queue = [body, info, solid]
    async def request(*args, **kwargs): return queue.pop(0)
    provider._request = request
    result = asyncio.run(provider.get_transaction(TXID))
    assert result.status == expected
    assert result.confirmed is bool(solid)
    if expected == "success":
        assert result.block_number == 5 and result.block_time == datetime.fromtimestamp(1, timezone.utc)


def test_tron_transaction_not_found_is_none():
    provider = bare_provider()
    async def request(*args, **kwargs): return {}
    provider._request = request
    assert asyncio.run(provider.get_transaction(TXID)) is None


def test_tron_transfer_log_decodes_event_and_hex_contract_to_base58():
    provider = bare_provider()
    topics = [
        "0x" + tron_module.TRANSFER_TOPIC,
        "0" * 24 + "1" * 40,
        "0" * 24 + "2" * 40,
    ]
    info = {"blockNumber": 42, "blockTimeStamp": 1_700_000_000_000,
            "log": [{"address": USDT_HEX[2:], "topics": topics, "data": f"{1_234_567:064x}", "logIndex": 8}]}
    solid = {"id": TXID}
    queue = [info, solid]
    async def request(*args, **kwargs): return queue.pop(0)
    async def contract(address):
        assert address == USDT
        return ContractInfo(True, "Tether", "USDT", 6)
    provider._request = request
    provider.get_contract_info = contract
    transfers = asyncio.run(provider.get_transaction_token_transfers(TXID))
    assert len(transfers) == 1
    assert transfers[0].token_address == USDT
    assert transfers[0].sender == TRON.address_from_hex("41" + "1" * 40)
    assert transfers[0].receiver == TRON.address_from_hex("41" + "2" * 40)
    assert transfers[0].amount == Decimal("1.234567") and transfers[0].confirmed


def test_getcontract_empty_object_means_not_a_contract():
    provider = bare_provider()
    async def request(*args, **kwargs): return {}
    provider._request = request
    result = asyncio.run(provider.get_contract_info(USDT))
    assert result == ContractInfo(False, None, None, None)


def test_tron_formatter_sanitizes_symbol_and_keeps_txid_without_0x():
    transfer = TronProvider._transfer_from_history_row(history_row(), WALLET)
    message = format_transfer_alert(transfer, TokenAnalyzer().analyze(transfer))
    assert "🌐 Ağ: TRON" in message.text
    assert f"<code>{TXID}</code>" in message.text
    assert "<code><b>USDT</b></code>" not in message.text
    assert "&lt;b&gt;USDT&lt;/b&gt;" in message.text
    assert all(len(button.get("callback_data", "").encode()) <= 64
               for row in (message.reply_markup or {}).get("inline_keyboard", []) for button in row)
