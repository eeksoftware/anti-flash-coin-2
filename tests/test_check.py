from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser

import pytest

from analyzer.token_analyzer import TokenAnalysis, TokenAnalyzer
from blockchain.etherscan import EtherscanClient
from blockchain.etherscan import EtherscanRateLimitError
from models.transaction import TokenTransfer
from monitor.transaction_tracker import TransactionTracker
from blockchain.base import BlockchainClientError
from monitor.wallet_monitor import HISTORY_WINDOW, WalletMonitor, history_page_size, spare_history_pages
from networks import get_network_config
from notifications.telegram_formatter import (
    MAX_TELEGRAM_TEXT_UNITS,
    format_check_error,
    format_check_prompt,
    format_check_usage,
    format_token_check,
    format_tx_check,
)
from providers.base import ChainProvider, ContractInfo, TransactionInfo
from providers.ethereum import EthereumProvider
from services.check_input import InvalidCheckInput, parse_check_args
from services.check_limits import CheckRateLimiter, TTLCache
from services.check_service import CheckService
from version import FALLBACK_VERSION, get_version


USDT = get_network_config("ethereum").official_usdt_contract.lower()
VARIANT = "0xc581b735a1688071a1746c968e0798d642ede491"
FRAUD = "0x0755bf4a88cf59aded7f8800ef34cca8a8105cfe"
WALLET = "0x" + "9" * 40
TX_HASH = "0x" + "a" * 64


def transfer(
    token: str = USDT,
    *,
    tx_hash: str = TX_HASH,
    symbol: str = "USDT",
    amount: str = "12.34",
    log_index: int = 1,
) -> TokenTransfer:
    return TokenTransfer(
        network="ethereum",
        tx_hash=tx_hash,
        log_index=log_index,
        block_number=21000000,
        timestamp=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc),
        token_address=token,
        token_symbol=symbol,
        token_name="Tether USD",
        decimals=6,
        amount=Decimal(amount),
        sender="0x" + "1" * 40,
        receiver="0x" + "2" * 40,
        direction="RECEIVE",
    )


class FakeProvider:
    network = "ethereum"

    def __init__(self):
        self.contracts: dict[str, ContractInfo] = {
            USDT: ContractInfo(True, "Tether USD", "USDT", 6, True),
            VARIANT: ContractInfo(True, "Euro Tether", "EURT", 6, True),
            FRAUD: ContractInfo(True, "USD Token", "USD𝖳", 18, False),
            WALLET: ContractInfo(False, None, None, None),
        }
        self.transaction: TransactionInfo | None = TransactionInfo(
            TX_HASH, "success", 21000000,
            datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc), 22,
            "0x" + "1" * 40, "0x" + "2" * 40,
        )
        self.transfers: list[TokenTransfer] = []
        self.contract_calls = 0
        self.transaction_calls = 0
        self.transfer_calls = 0

    async def get_token_transfers(self, address: str, limit: int):
        return []

    async def get_transaction(self, tx_hash: str):
        self.transaction_calls += 1
        return self.transaction

    async def get_transaction_token_transfers(self, tx_hash: str):
        self.transfer_calls += 1
        return self.transfers[:10]

    async def get_contract_info(self, token_address: str):
        self.contract_calls += 1
        return self.contracts.get(token_address.lower(), ContractInfo(True, "Unknown", "UNK", 18))


@pytest.fixture
def setup_service():
    provider = FakeProvider()
    return provider, CheckService({"ethereum": provider}, TokenAnalyzer())


def test_parse_check_valid_hash_address_prefix_and_normalization():
    assert parse_check_args([TX_HASH.upper().replace("0X", "0x")]).kind == "transaction"
    assert parse_check_args([USDT.upper().replace("0X", "0x")]).value == USDT
    assert parse_check_args(["Ethereum", USDT]).network == "ethereum"


@pytest.mark.parametrize("args", [[], ["bad"], ["ethereum", "bad"], ["tron", USDT], ["x" * 201]])
def test_parse_check_rejects_invalid_or_unknown_input(args):
    with pytest.raises((InvalidCheckInput, ValueError)):
        parse_check_args(args)


def test_service_classifies_canonical_variant_known_fraud_and_wallet(setup_service):
    _, service = setup_service
    canonical = asyncio.run(service.check_token("ethereum", USDT))
    variant = asyncio.run(service.check_token("ethereum", VARIANT))
    fraud = asyncio.run(service.check_token("ethereum", FRAUD))
    wallet = asyncio.run(service.check_token("ethereum", WALLET))
    assert canonical.analysis.classification == "CANONICAL"
    assert variant.analysis.classification == "VARIANT"
    assert fraud.analysis.classification == "KNOWN_FRAUD"
    assert wallet.is_wallet and wallet.analysis is None


def test_service_checks_transaction_transfers_status_missing_and_empty(setup_service):
    provider, service = setup_service
    provider.transfers = [transfer(USDT), transfer(FRAUD, log_index=2)]
    checked = asyncio.run(service.check_transaction("ethereum", TX_HASH))
    assert [item.analysis.classification for item in checked.transfers] == ["CANONICAL", "KNOWN_FRAUD"]
    assert checked.classification == "KNOWN_FRAUD"

    provider.transaction = replace(provider.transaction, status="failed")
    failed = asyncio.run(service.check_transaction("ethereum", "0x" + "b" * 64))
    assert failed.transaction.status == "failed"

    provider.transaction = None
    missing = asyncio.run(service.check_transaction("ethereum", "0x" + "c" * 64))
    assert missing.transaction is None
    assert missing.classification == "UNKNOWN"

    provider.transaction = TransactionInfo(
        TX_HASH, "success", 21000000,
        datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc), 22,
        "0x" + "1" * 40, "0x" + "2" * 40,
    )
    provider.transfers = []
    empty = asyncio.run(service.check_transaction("ethereum", "0x" + "d" * 64))
    assert not empty.transfers


def test_cache_avoids_second_provider_request():
    now = [100.0]
    provider = FakeProvider()
    service = CheckService({"ethereum": provider}, TokenAnalyzer(), TTLCache(lambda: now[0]))
    first = asyncio.run(service.check_token("ethereum", USDT))
    second = asyncio.run(service.check_token("ethereum", USDT))
    assert first.cached is False and second.cached is True
    assert provider.contract_calls == 1
    now[0] += 601
    asyncio.run(service.check_token("ethereum", USDT))
    assert provider.contract_calls == 2


def test_check_rate_limiter_enforces_spacing_and_rolling_minute():
    now = [0.0]
    limiter = CheckRateLimiter(lambda: now[0])
    assert limiter.allow("user")
    now[0] = 2.99
    assert not limiter.allow("user")
    now[0] = 3.0
    assert limiter.allow("user")
    for index in range(8):
        now[0] += 3.0
        assert limiter.allow("user")
    now[0] += 3.0
    assert not limiter.allow("user")
    now[0] = 60.0
    assert limiter.allow("user")


class BalancedHTML(HTMLParser):
    ALLOWED = {"b", "i", "code"}

    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in self.ALLOWED
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag


def assert_balanced(text: str) -> None:
    parser = BalancedHTML()
    parser.feed(text)
    parser.close()
    assert not parser.stack


def test_token_check_escapes_untrusted_metadata_and_shows_canonical_result(setup_service):
    provider, service = setup_service
    provider.contracts[USDT] = ContractInfo(True, "Name\n", "<b>x</b>&\n", 6)
    result = asyncio.run(service.check_token("ethereum", USDT))
    message = format_token_check(result)
    assert "&lt;b&gt;x&lt;/b&gt;&amp;" in message.text
    assert "<code><b>x</b>" not in message.text
    assert f"<code>{USDT}</code>" in message.text
    assert "Yön" not in message.text
    assert_balanced(message.text)


def test_wallet_check_has_requested_short_explanation(setup_service):
    _, service = setup_service
    message = format_token_check(asyncio.run(service.check_token("ethereum", WALLET)))
    assert "Bu bir cüzdan adresi, token kontratı değil." in message.text
    assert "/watch" in message.text and "/history" in message.text


def test_transaction_check_shows_status_transfer_limit_and_full_code_addresses(setup_service):
    provider, service = setup_service
    provider.transfers = [transfer(tx_hash=TX_HASH, log_index=i) for i in range(7)]
    result = asyncio.run(service.check_transaction("ethereum", TX_HASH))
    message = format_tx_check(result)
    assert "✅ Başarılı" in message.text
    assert "+2 transfer daha" in message.text
    assert f"📤 Gönderen: <code>{'0x' + '1' * 40}</code>" in message.text
    assert f"📥 Alıcı: <code>{'0x' + '2' * 40}</code>" in message.text
    assert "Yön" not in message.text
    assert_balanced(message.text)


def test_transaction_check_links_suspicious_token_even_after_first_five(setup_service):
    provider, service = setup_service
    provider.transfers = [
        transfer(USDT, log_index=index) for index in range(5)
    ] + [transfer(FRAUD, log_index=6)]
    result = asyncio.run(service.check_transaction("ethereum", TX_HASH))
    message = format_tx_check(result)
    urls = [
        button.get("url", "")
        for row in message.reply_markup["inline_keyboard"]
        for button in row
    ]
    assert any(url.endswith(f"/token/{FRAUD}") for url in urls)


def test_transaction_missing_and_failed_results_are_formatted(setup_service):
    provider, service = setup_service
    provider.transaction = None
    missing = asyncio.run(service.check_transaction("ethereum", "0x" + "f" * 64))
    assert "İşlem bulunamadı" in format_tx_check(missing).text

    provider.transaction = TransactionInfo("0x" + "f" * 64, "failed", 20, None, 0, "0x1", None)
    failed = asyncio.run(service.check_transaction("ethereum", "0x" + "a" * 64))
    assert "❌ Başarısız" in format_tx_check(failed).text
    provider.transaction = replace(provider.transaction, status="success")
    provider.transfers = []
    empty = asyncio.run(service.check_transaction("ethereum", "0x" + "e" * 64))
    assert "ERC-20 token transferi bulunamadı" in format_tx_check(empty).text


def test_check_messages_are_limited_to_4096_utf16_units_and_use_no_direction_label(setup_service):
    provider, service = setup_service
    provider.transfers = [replace(transfer(log_index=i), sender="<" + "x" * 2000) for i in range(10)]
    result = asyncio.run(service.check_transaction("ethereum", TX_HASH))
    messages = [
        format_check_usage(), format_check_prompt(), format_check_error(),
        format_check_error("not_found"), format_token_check(asyncio.run(service.check_token("ethereum", USDT))),
        format_tx_check(result),
    ]
    for message in messages:
        assert len(message.text.encode("utf-16-le")) // 2 <= MAX_TELEGRAM_TEXT_UNITS
        assert "Yön" not in message.text
        assert_balanced(message.text)


def test_ethereum_provider_satisfies_provider_protocol_without_network_calls():
    provider = EthereumProvider(EtherscanClient())
    assert isinstance(provider, ChainProvider)


class FakeEtherscanClient:
    def __init__(self):
        address_topic = lambda char: "0x" + "0" * 24 + char * 40
        self.logs = [
            {
                "address": USDT,
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    address_topic("1"), address_topic("2"),
                ],
                "data": hex(1234567), "logIndex": hex(index),
            }
            for index in range(11)
        ]
        self.logs.insert(0, {
            "address": USDT,
            "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"] * 4,
            "data": hex(999), "logIndex": "0x99",
        })
        self.block_requests = 0

    async def get_transaction_receipt(self, tx_hash):
        return {"blockNumber": "0x20", "logs": self.logs}

    async def get_block(self, block_number):
        self.block_requests += 1
        return {"timestamp": "0x65000000"}

    async def get_code(self, address):
        return "0x6000"

    async def eth_call(self, address, selector):
        if selector == "0x313ce567":
            return "0x" + f"{6:064x}"
        return _abi_string("USDT" if selector == "0x95d89b41" else "Tether USD")

    async def get_contract_source(self, address):
        return {"SourceCode": "contract Tether {}"}

    async def get_contract_creation(self, address):
        return {"contractCreator": "0x" + "3" * 40, "txHash": TX_HASH}

    async def close(self):
        return None


def _abi_string(value: str) -> str:
    encoded = value.encode().hex()
    padded = encoded.ljust(((len(encoded) + 63) // 64) * 64, "0")
    return "0x" + f"{32:064x}" + f"{len(value):064x}" + padded


def test_ethereum_provider_decodes_erc20_logs_skips_nft_and_caps_at_ten():
    provider = EthereumProvider(FakeEtherscanClient())
    records = asyncio.run(provider.get_transaction_token_transfers(TX_HASH))
    assert len(records) == 10
    assert records[0].amount == Decimal("1.234567")
    assert records[0].decimals == 6
    assert records[0].sender == "0x" + "1" * 40
    assert records[0].receiver == "0x" + "2" * 40
    assert records[0].direction == "UNKNOWN"
    assert provider.client.block_requests == 1


def test_provider_does_not_swallow_etherscan_rate_limit():
    class RateLimited(FakeEtherscanClient):
        async def eth_call(self, address, selector):
            raise EtherscanRateLimitError("rate limited")

    with pytest.raises(EtherscanRateLimitError):
        asyncio.run(EthereumProvider(RateLimited()).get_contract_info(USDT))


def test_get_version_prefers_environment_then_git_then_fallback(monkeypatch):
    import version

    monkeypatch.setenv("BOT_VERSION", "2.3.4-test")
    assert get_version() == "2.3.4-test"
    monkeypatch.delenv("BOT_VERSION")
    monkeypatch.setattr(version, "_git_output", lambda *args: "baseline-v0-2-gabc1234")
    assert get_version() == "baseline-v0-2-gabc1234"
    monkeypatch.setattr(version, "_git_output", lambda *args: None)
    assert get_version() == FALLBACK_VERSION


@pytest.mark.parametrize("token", [USDT, VARIANT, FRAUD, "0x" + "e" * 40])
def test_stage_zero_analyzer_entrypoint_preserves_existing_classification(token):
    analyzer = TokenAnalyzer()
    original = analyzer.analyze(transfer(token))
    via_provider = analyzer.analyze_token("ethereum", token, "USDT")
    assert original == via_provider


def test_stage_zero_analyzer_golden_classifications_remain_stable():
    analyzer = TokenAnalyzer()
    expected = {
        USDT: "CANONICAL",
        VARIANT: "VARIANT",
        FRAUD: "KNOWN_FRAUD",
        "0x" + "e" * 40: "UNKNOWN",
    }
    assert {
        address: analyzer.analyze(transfer(address)).classification
        for address in expected
    } == expected


def test_wallet_monitor_using_fake_provider_keeps_golden_classifications():
    first = transfer(USDT, tx_hash="0x" + "1" * 64)
    next_transfers = [
        first,
        transfer(FRAUD, tx_hash="0x" + "2" * 64, log_index=2),
    ]

    class MonitoringProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.polls = 0

        async def get_token_transfer_page(self, address, page, limit):
            self.polls += 1
            return [first] if self.polls == 1 else next_transfers

    provider = MonitoringProvider()
    analyzer = TokenAnalyzer()
    observed: list[str] = []
    detected = asyncio.Event()

    async def on_transfers(batch):
        observed.extend(analyzer.analyze(item).classification for item in batch)
        detected.set()

    monitor = WalletMonitor(
        client=provider,
        tracker=TransactionTracker(),
        wallet_address=WALLET,
        poll_interval=0.01,
        max_poll_interval=0.02,
        on_transfers=on_transfers,
    )

    async def run_monitor():
        task = asyncio.create_task(monitor.run())
        try:
            await asyncio.wait_for(detected.wait(), timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run_monitor())
    assert observed == ["KNOWN_FRAUD"]


def test_wallet_monitor_continues_after_transfer_handler_fails():
    first = transfer(USDT, tx_hash="0x" + "1" * 64)
    failed = transfer(USDT, tx_hash="0x" + "2" * 64, log_index=2)
    later = transfer(FRAUD, tx_hash="0x" + "3" * 64, log_index=3)

    class MonitoringProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.polls = 0

        async def get_token_transfer_page(self, address, page, limit):
            self.polls += 1
            if self.polls == 1:
                return [first]
            if self.polls == 2:
                return [first, failed]
            return [first, failed, later]

    seen: list[str] = []
    detected = asyncio.Event()

    async def on_transfers(batch):
        if any(item.tx_hash == failed.tx_hash for item in batch):
            raise TypeError("type NoneType doesn't define __round__ method")
        seen.extend(item.tx_hash for item in batch)
        detected.set()

    monitor = WalletMonitor(
        client=MonitoringProvider(),
        tracker=TransactionTracker(),
        wallet_address=WALLET,
        poll_interval=0.01,
        max_poll_interval=0.02,
        on_transfers=on_transfers,
    )

    async def run_monitor():
        task = asyncio.create_task(monitor.run())
        try:
            await asyncio.wait_for(detected.wait(), timeout=2)
            assert not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run_monitor())
    assert seen == [later.tx_hash]


class _Key:
    def __init__(self, calls=0, cooldown_until=0.0):
        self.disabled = False
        self.exhausted = False
        self.calls = calls
        self.cooldown_until = cooldown_until


class _Pool:
    def __init__(self, daily_limit, keys):
        self._daily_limit = daily_limit
        self._keys = keys


def test_spare_history_pages_follows_daily_budget_and_cooldown():
    noon = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    plenty = spare_history_pages(
        _Pool(100_000, [_Key(), _Key()]),
        wallets=2, reserve_interval=30, approval_interval=60, page_size=1000,
        now=noon, reset_hour=0, clock=lambda: 0,
    )
    assert plenty == (HISTORY_WINDOW // 1000) - 1
    tight = spare_history_pages(
        _Pool(100, [_Key()]),
        wallets=2, reserve_interval=30, approval_interval=60, page_size=1000,
        now=noon, reset_hour=0, clock=lambda: 0,
    )
    assert tight == 0
    cooling = spare_history_pages(
        _Pool(100_000, [_Key(cooldown_until=50)]),
        wallets=1, reserve_interval=30, approval_interval=None, page_size=1000,
        now=noon, reset_hour=0, clock=lambda: 0,
    )
    assert cooling == 0
    assert spare_history_pages(None, wallets=1, reserve_interval=30) == 0
    assert history_page_size("ethereum") == 1000
    assert history_page_size("tron") == 200


def _history_batch(page: int, count: int) -> list[TokenTransfer]:
    return [
        transfer(USDT, tx_hash="0x" + f"{page:02x}{index:062x}", log_index=index)
        for index in range(count)
    ]


def test_baseline_fetches_until_short_page_when_budget_allows():
    class Provider:
        network = "ethereum"
        pool = _Pool(100_000, [_Key()])

        def __init__(self):
            self.pages = []

        async def get_transaction(self, tx_hash):
            return None

        async def get_token_transfer_page(self, address, page, limit):
            self.pages.append((page, limit))
            if page < 3:
                return _history_batch(page, limit)
            return _history_batch(page, 1)

    provider = Provider()
    seen = []

    async def on_baseline(items):
        seen.append(len(items))

    async def on_transfers(items):
        raise AssertionError("baseline history must not alert")

    monitor = WalletMonitor(
        provider, TransactionTracker(), WALLET, 0.01, 0.02,
        on_transfers=on_transfers, on_baseline=on_baseline,
    )

    async def run():
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert provider.pages[:3] == [(1, 1000), (2, 1000), (3, 1000)]
    assert seen == [2001]


def test_baseline_stops_at_one_page_when_daily_budget_is_reserved():
    class Provider:
        network = "ethereum"
        pool = _Pool(10, [_Key()])

        def __init__(self):
            self.pages = []

        async def get_transaction(self, tx_hash):
            return None

        async def get_token_transfer_page(self, address, page, limit):
            self.pages.append(page)
            return _history_batch(page, limit)

    provider = Provider()

    async def on_baseline(items):
        return None

    monitor = WalletMonitor(
        provider, TransactionTracker(), WALLET, 0.01, 0.02, on_baseline=on_baseline,
    )

    async def run():
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert provider.pages == [1]


def test_baseline_keeps_partial_history_when_a_later_page_fails():
    fresh = transfer(FRAUD, tx_hash="0x" + "f" * 64, log_index=9)

    class Provider:
        network = "ethereum"
        pool = _Pool(100_000, [_Key()])

        def __init__(self):
            self.started = False

        async def get_transaction(self, tx_hash):
            return None

        async def get_token_transfer_page(self, address, page, limit):
            if not self.started:
                if page == 1:
                    return _history_batch(1, limit)
                self.started = True
                raise BlockchainClientError("Etherscan istek sınırına ulaşıldı.")
            return [fresh]

    provider = Provider()
    baselined = []
    alerted = []
    detected = asyncio.Event()

    async def on_baseline(items):
        baselined.append(len(items))

    async def on_transfers(items):
        alerted.extend(item.tx_hash for item in items)
        detected.set()

    monitor = WalletMonitor(
        provider, TransactionTracker(), WALLET, 0.01, 0.02,
        on_transfers=on_transfers, on_baseline=on_baseline,
    )

    async def run():
        task = asyncio.create_task(monitor.run())
        try:
            await asyncio.wait_for(detected.wait(), timeout=2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert baselined == [1000]
    assert alerted == [fresh.tx_hash]
