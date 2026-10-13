import os
import sys
from pathlib import Path


# Keep `python3 main.py` as the only startup command: use this project's
# installed dependencies and macOS certificate bundle automatically.
os.environ.setdefault("SSL_CERT_FILE", "/etc/ssl/cert.pem")
PROJECT_ROOT = Path(__file__).resolve().parent
PROJECT_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
if sys.prefix == sys.base_prefix and PROJECT_PYTHON.is_file():
    os.execv(str(PROJECT_PYTHON), [str(PROJECT_PYTHON), *sys.argv])


import asyncio
import hashlib
import logging
import shutil
import signal
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import aiohttp

from audit_log import (
    CLASSIFICATION_LABELS,
    audit_event_key,
    configure_logging,
    flush_logging,
    get_run_id,
    log_transaction,
    set_bot_version,
    shutdown_logging,
    take_interval_counts,
)
from health import (GREEN, YELLOW, RED, HealthState, OperationsStore, health_level, write_heartbeat, write_atomic_json)
from analyzer import address_poisoning
from analyzer.risk_engine import classification_signals, compute_risk
from analyzer.token_analyzer import TokenAnalyzer
from blockchain.etherscan import EtherscanClient
from blockchain.normalizer import normalize_etherscan_transfers
from blockchain.token_price import DexScreenerPriceClient
from config import settings
from models.transaction import TokenTransfer
from monitor.approval_monitor import ApprovalMonitor
from reporting.chat_store import ChatStore
from reporting.weekly import WeeklyReporter
from monitor.state_db import SUSPICIOUS_CLASSIFICATIONS, UserStateStore, skip_notification
from notifications.fee_alerts import fee_action, quote_usd, stable_unit_price
from monitor.wallet_monitor import WalletMonitorManager
from monitor.wallet_store import WalletStateStore
from notifications.telegram import (
    TelegramBotController,
    TelegramError,
    TelegramNotifier,
)
from notifications.admin import parse_admin_ids
from providers.ethereum import EthereumProvider
from providers.tron import TronProvider
from services.check_service import CheckService
from version import get_git_commit, get_version


def _display_time(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if value else "N/A"


def build_health_report(*, state, provider, store, operations, version, commit, data_dir, log_dir):
    provider_map = provider if isinstance(provider, dict) else {getattr(provider, "network", "ethereum"): provider}
    keys = []
    cooldown = exhausted = active = 0
    for network, item in provider_map.items():
        client = getattr(item, "client", item)
        pool = getattr(client, "pool", None)
        if pool is None:
            continue
        daily_limit = settings.trongrid_daily_limit if network == "tron" else settings.etherscan_daily_limit
        for key in pool._keys:
            if key.disabled:
                status = "DISABLED"
            elif key.exhausted:
                status = "EXHAUSTED"; exhausted += 1
            elif key.cooldown_until > time.monotonic():
                status = "COOLDOWN"; cooldown += 1
            else:
                status = "ACTIVE"; active += 1
            usage = key.calls / max(1, daily_limit) * 100
            keys.append({"network": network, "label": key.label, "state": status, "usage": f"{key.calls:,}/{daily_limit:,} (%{usage:.1f})"})
    log_path = Path(log_dir); data_path = Path(data_dir)
    measurement_ok = True
    try:
        log_size = sum(item.stat().st_size for item in log_path.rglob("*") if item.is_file()) if log_path.exists() else 0
        disk_targets = [path for path in (data_path, log_path) if path.exists()]
        disks = [shutil.disk_usage(path) for path in disk_targets] or [shutil.disk_usage(data_path.parent)]
        disk_ratios = [disk.free / disk.total for disk in disks if disk.total]
        disk_ratio = min(disk_ratios) if disk_ratios else None
        free_bytes = min(disk.free for disk in disks)
        disk_free = f"{free_bytes / (1024**3):.2f} GB (%{(disk_ratio or 0)*100:.1f})"
        log_size_text = f"{log_size / (1024**2):.2f} MB"
    except OSError:
        measurement_ok = False; disk_ratio = None
        disk_free = log_size_text = "N/A"
    level = health_level(state, active_keys=active, cooldown_keys=cooldown,
                         exhausted_keys=exhausted, disk_free_ratio=disk_ratio,
                         measurements_ok=measurement_ok)
    labels = {GREEN: "Normal", YELLOW: "Uyarı", RED: "Kritik"}
    uptime = int(state.as_dict()["uptime_seconds"])
    wallets = len({(x.network, x.wallet_address) for x in store.all_subscriptions()}) if store else "N/A"
    return {"icon": level, "label": labels[level], "version": version or "unknown", "commit": commit or "unknown",
            "uptime": f"{uptime//86400}g {(uptime%86400)//3600}s {(uptime%3600)//60}d",
            "wallet_count": wallets, "last_cycle": _display_time(state.last_cycle_at), "cycle_count": state.cycle_count,
            "interval": f"{state.effective_interval:.1f} sn" if state.effective_interval is not None else "N/A",
            "last_error": (state.last_error or "Yok")[:160], "last_api": _display_time(state.last_api_success_at),
            "last_telegram": _display_time(state.last_telegram_success_at), "log_size": log_size_text,
            "disk_free": disk_free, "unexpected_restarts": operations.unexpected_restarts_7d() if operations else "N/A", "keys": keys}


def build_stats_report(operations, client):
    daily, rolling = operations.stats() if operations else ({}, {})
    classes = ("CANONICAL", "VARIANT", "KNOWN_FRAUD", "UNKNOWN", "ERROR")
    def period(values):
        total = values.get(("transfer_total", ""), 0)
        class_rows = [{"name": name, "count": values.get((f"classification:{name}", ""), 0),
                       "percent": f"{(values.get((f'classification:{name}', '') , 0)/total*100 if total else 0):.1f}"} for name in classes]
        return {"transfer_total": total, "suspicious_total": values.get(("suspicious_total", ""), 0),
                "notifications_sent": values.get(("notifications_sent", ""), 0),
                "errors": values.get(("errors", ""), 0), "check_usage": values.get(("check_usage", ""), 0), "classes": class_rows}
    provider_map = client if isinstance(client, dict) else {getattr(client, "network", "ethereum"): client}
    primary_network = "ethereum" if "ethereum" in provider_map else next(iter(provider_map), "ethereum")
    primary_provider = provider_map.get(primary_network)
    client = getattr(primary_provider, "client", primary_provider)
    metrics = getattr(client, "metrics", None)
    snapshot = metrics.snapshot() if metrics else {}
    pool = getattr(client, "pool", None)
    calls_by_key = snapshot.get("today")
    key_rows = []
    if pool is not None and isinstance(calls_by_key, dict):
        for key in pool._keys:
            calls = key.calls
            limit = settings.trongrid_daily_limit if primary_network == "tron" else settings.etherscan_daily_limit
            key_rows.append({"label": key.label, "calls": calls, "limit": limit,
                             "percent": f"{calls/max(1,limit)*100:.1f}"})
    def value(name, unit=""):
        result = snapshot.get(name)
        if not isinstance(result, (int,float)): return "N/A"
        return f"{result*1000:.1f} ms" if unit == "ms" else (f"{result*100:.1f}%" if unit == "%" else result)
    has_api_samples = bool(snapshot.get("keys"))
    rate_limited = "N/A"
    if has_api_samples and isinstance(calls_by_key, dict):
        rate_limited = sum(row.get("rate_limited", 0) for row in calls_by_key.values())
    cache_total = snapshot.get("cache_hits", 0) + snapshot.get("cache_misses", 0)
    api_providers = {}
    for network, provider in provider_map.items():
        api_client = getattr(provider, "client", provider)
        api_metrics = getattr(api_client, "metrics", None)
        api_snapshot = api_metrics.snapshot() if api_metrics else {}
        api_pool = getattr(api_client, "pool", None)
        if network == primary_network:
            api_providers[network] = {"keys": key_rows, "rate_limited": rate_limited,
                "latency_avg": value("latency_avg", "ms") if has_api_samples else "N/A",
                "latency_p95": value("latency_p95", "ms") if has_api_samples else "N/A",
                "cache_hit_ratio": value("cache_hit_ratio", "%") if cache_total else "N/A"}
            continue
        rate_count = sum(row.get("rate_limited", 0) for row in api_snapshot.get("today", {}).values())
        calls = api_snapshot.get("today", {})
        rows = []
        if api_pool is not None:
            for key in api_pool._keys:
                rows.append({"label": key.label, "calls": key.calls, "limit": settings.trongrid_daily_limit,
                             "percent": f"{key.calls/max(1,settings.trongrid_daily_limit)*100:.1f}"})
        api_providers[network] = {"keys": rows, "rate_limited": rate_count,
            "latency_avg": f"{api_snapshot['latency_avg']*1000:.1f} ms" if calls else "N/A",
            "latency_p95": f"{api_snapshot['latency_p95']*1000:.1f} ms" if calls else "N/A",
            "cache_hit_ratio": f"{api_snapshot['cache_hit_ratio']*100:.1f}%" if api_snapshot.get("cache_hits", 0)+api_snapshot.get("cache_misses", 0) else "N/A"}
    return {"today": period(daily), "last24h": period(rolling),
            "api": {"keys": key_rows, "rate_limited": rate_limited,
                    "latency_avg": value("latency_avg", "ms") if has_api_samples else "N/A",
                    "latency_p95": value("latency_p95", "ms") if has_api_samples else "N/A",
                    "cache_hit_ratio": value("cache_hit_ratio", "%") if cache_total else "N/A",
                    "providers": api_providers, "primary_network": primary_network}}


async def _transfer_usd(transfer, classification: str, price_client, price_cache: dict) -> Decimal | None:
    """USD value for the fee cutoff. A missing market price returns None."""
    price = stable_unit_price(
        transfer.network, transfer.token_address, classification, transfer.token_symbol,
    )
    if price is None and transfer.network == "ethereum" and transfer.token_address:
        address = str(transfer.token_address).lower()
        now = time.monotonic()
        cached = price_cache.get(address)
        if cached is None or now - cached[0] >= 300:
            fetched = None
            try:
                prices = await price_client.get_usd_prices([address])
                fetched = prices.get(address)
            except Exception:
                logging.getLogger(__name__).warning("USD fiyatı alınamadı.")
            price_cache[address] = (now, fetched)
            price = fetched
        else:
            price = cached[1]
    return quote_usd(transfer.amount, price)


async def main() -> None:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    installed_signals = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
            installed_signals.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    data_dir = Path(settings.data_dir)
    log_dir = Path(settings.log_dir)
    if not data_dir.is_absolute(): data_dir = PROJECT_ROOT / data_dir
    if not log_dir.is_absolute(): log_dir = PROJECT_ROOT / log_dir
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(
        str(log_dir),
        settings.log_level,
        settings.log_retention_days,
        data_dir=str(data_dir),
    )
    logger = logging.getLogger(__name__)
    started_at = time.monotonic()
    bot_version = get_version()
    health_state = HealthState.now()
    operations = None
    user_state = None
    run_session_id = None
    clean_shutdown = False
    try:
        operations = OperationsStore(data_dir / "operations.sqlite")
        run_session_id, stale_count = operations.start_session(bot_version)
        if stale_count:
            logger.warning("Önceki çalışma oturumu beklenmedik kapanmış olarak işaretlendi | sayi=%d", stale_count)
    except Exception:
        logger.exception("Operasyon veritabanı açılamadı; sağlık ve istatistik kalıcılığı kullanılamayacak.")
    set_bot_version(bot_version)
    logger.info(
        "Bot başlatıldı | sürüm=%s | git_commit=%s | run_id=%s",
        bot_version,
        get_git_commit(),
        get_run_id(),
    )

    try:
        wallet_store = WalletStateStore()
        try:
            user_state = UserStateStore(data_dir / "user_state.sqlite")
        except Exception:
            logger.exception("Kullanıcı ayar veritabanı açılamadı; varsayılan bildirim davranışı kullanılacak.")
        if settings.network in settings.enabled_networks:
            wallet_store.ensure_legacy_wallet(settings.network, settings.monitored_wallet)

        subscriptions = wallet_store.all_subscriptions()
        logger.info(
            "İzlenen cüzdan sayısı: %d",
            len({(item.network, item.wallet_address) for item in subscriptions}),
        )

        providers = {}
        client = None
        provider = None
        if "ethereum" in settings.enabled_networks:
            client = EtherscanClient()
            client.health_state = health_state
            provider = EthereumProvider(client)
            providers["ethereum"] = provider
        if "tron" in settings.enabled_networks:
            tron_provider = TronProvider()
            tron_provider.health_state = health_state
            providers["tron"] = tron_provider
        price_client = DexScreenerPriceClient()
        price_cache: dict[str, tuple[float, Decimal | None]] = {}
        token_analyzer = TokenAnalyzer()
        check_service = CheckService(providers, token_analyzer)
        telegram = None
        if settings.telegram_bot_token:
            telegram = TelegramNotifier(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
                health_state,
                chat_store=ChatStore(data_dir / "chat_messages.sqlite"),
            )
        else:
            logger.info("Telegram bildirimleri kapalı; bot bilgileri yapılandırılmamış.")

        async def handle_new_transfers(subscription, transfers: list[TokenTransfer]) -> None:
            for transfer in transfers:
                try:
                    analysis_started = time.perf_counter()
                    analysis = None
                    analysis_error = None
                    try:
                        analysis = token_analyzer.analyze(transfer)
                    except Exception as exc:
                        analysis_error = str(exc)
                        logger.exception("Token transferi analiz edilemedi.")
                    processing_ms = (time.perf_counter() - analysis_started) * 1000

                    classification = analysis.classification if analysis is not None else "ERROR"
                    if operations:
                        increments = [("transfer_total", "", 1), (f"classification:{classification}", "", 1)]
                        if classification in SUSPICIOUS_CLASSIFICATIONS:
                            increments.append(("suspicious_total", "", 1))
                        if analysis_error:
                            increments.append(("errors", "", 1))
                        event_fingerprint = hashlib.sha256(repr(transfer.event_id).encode("utf-8")).hexdigest()
                        operations.increment_many_once(event_fingerprint, increments)
                    label = CLASSIFICATION_LABELS.get(classification, "Analiz hatası")
                    logger.info(
                        "%s %s %s olarak sınıflandırıldı: %s",
                        "Alındı" if transfer.direction == "RECEIVE" else "Gönderildi",
                        transfer.amount,
                        transfer.token_symbol,
                        label,
                    )

                    # Stage 7: risk signals sit beside the classification; they never change it.
                    audit_record = {
                        "network": transfer.network,
                        "tx_hash": transfer.tx_hash,
                        "log_index": transfer.log_index,
                        "event_key": transfer.event_key,
                        "token_address": transfer.token_address,
                        "wallet": subscription.wallet_address,
                    }
                    counterparty = transfer.sender if transfer.direction == "RECEIVE" else transfer.receiver
                    poisoning = None
                    if user_state is not None:
                        try:
                            known = user_state.counterparties(transfer.network, subscription.wallet_address)
                            poisoning = address_poisoning.evaluate(
                                transfer.network, subscription.wallet_address, counterparty,
                                transfer.amount, known, event_key=transfer.event_key,
                            )
                        except Exception:
                            logger.warning("Adres benzerliği değerlendirilemedi.")
                    risk_signals = classification_signals(analysis, network=transfer.network, event_key=transfer.event_key)
                    if poisoning is not None:
                        risk_signals.extend(poisoning.signals)
                        logger.info("Adres benzerliği şüphesi | baş=%d | son=%d", poisoning.similarity.prefix, poisoning.similarity.suffix)
                    risk = compute_risk(risk_signals, missing=("classification",) if analysis is None else ())
                    if user_state is not None:
                        try:
                            fresh = user_state.save_detection(
                                event_key=audit_event_key(audit_record), kind="risk", network=transfer.network,
                                wallet_address=subscription.wallet_address, tx_hash=transfer.tx_hash,
                                score=risk.score, level=risk.level, weights_version=risk.weights_version,
                                signals=[{"name": s.name, "weight": s.weight, "verified": s.verified, "evidence": s.evidence} for s in risk.signals],
                            )
                            if fresh:
                                user_state.record_counterparties(transfer.network, subscription.wallet_address, [counterparty], transfer.timestamp)
                        except Exception:
                            logger.warning("Risk tespiti kaydedilemedi.")
                    extra_suspicious = poisoning is not None or risk.level in {"YÜKSEK", "KRİTİK"}

                    notified = None
                    telegram_message_id = None
                    notify_error = None
                    if analysis is not None and telegram is not None:
                        recipient = subscription.user_id or settings.telegram_chat_id
                        alias = None
                        feedback_token = None
                        prefs = None
                        send_alert = True
                        fee_kind = None
                        if subscription.user_id and user_state is not None:
                            try:
                                prefs = user_state.settings(
                                    subscription.user_id, transfer.network, subscription.wallet_address,
                                )
                            except Exception:
                                logger.warning("Cüzdan ayarları okunamadı; varsayılan bildirim kullanılacak.")
                                prefs = None
                            if prefs is not None:
                                alias = prefs.alias or None
                                reason = skip_notification(
                                    classification, transfer.amount, prefs, datetime.now(timezone.utc),
                                    extra_suspicious=extra_suspicious,
                                )
                                if reason:
                                    send_alert = False
                                    logger.info("Bildirim atlandı | neden=%s", reason)
                        if send_alert:
                            suspicious = extra_suspicious or classification in SUSPICIOUS_CLASSIFICATIONS
                            usd = await _transfer_usd(transfer, classification, price_client, price_cache)
                            fee_kind = fee_action(
                                usd,
                                suspicious=suspicious,
                                notify_fees=bool(prefs and prefs.fee_alerts),
                            )
                            if fee_kind == "skip":
                                send_alert = False
                                logger.info("Bildirim atlandı | neden=fee")
                            elif fee_kind != "fee" and prefs is not None and subscription.user_id and user_state is not None:
                                try:
                                    feedback_token = user_state.register_alert(
                                        user_id=subscription.user_id,
                                        network=transfer.network,
                                        wallet_address=subscription.wallet_address,
                                        classification=classification,
                                        record=audit_record,
                                    )
                                except Exception:
                                    logger.warning("Geri bildirim kaydı oluşturulamadı.")
                        if send_alert and recipient:
                            try:
                                address_names = {}
                                if transfer.network == "ethereum" and client is not None:
                                    address_names = await client.get_address_tags([
                                        transfer.sender, transfer.receiver, transfer.token_address,
                                    ])
                                if fee_kind == "fee":
                                    result = await telegram.send_fee_alert(
                                        transfer, usd, chat_id=recipient, alias=alias, address_names=address_names,
                                    )
                                else:
                                    result = await telegram.send_transfer_alert(
                                        transfer,
                                        analysis,
                                        chat_id=recipient,
                                        alias=alias,
                                        feedback_token=feedback_token,
                                        risk=risk,
                                        poisoning=poisoning,
                                        address_names=address_names,
                                    )
                                notified = True
                                if operations: operations.increment("notifications_sent")
                                if isinstance(result, dict):
                                    telegram_message_id = result.get("message_id")
                            except Exception as exc:
                                notified = False
                                if operations: operations.increment("errors")
                                notify_error = str(exc)
                                logger.error("Telegram bildirimi gönderilemedi: %s", exc)

                    recorded_at = datetime.now(timezone.utc)
                    block_time = transfer.timestamp
                    if block_time.tzinfo is None:
                        block_time = block_time.replace(tzinfo=timezone.utc)
                    detection_latency_s = (
                        max(
                            0.0,
                            (recorded_at - block_time.astimezone(timezone.utc)).total_seconds(),
                        )
                        if classification != "CANONICAL"
                        else None
                    )
                    signals = [signal.name for signal in risk.signals]
                    log_transaction({
                        "ts": recorded_at,
                        "source": "monitor",
                        "network": transfer.network,
                        "tx_hash": transfer.tx_hash,
                        "log_index": transfer.log_index,
                        "event_key": transfer.event_key,
                        "block_number": transfer.block_number,
                        "block_time": block_time,
                        "direction": transfer.direction,
                        "wallet": subscription.wallet_address,
                        "sender": transfer.sender,
                        "receiver": transfer.receiver,
                        "token_address": transfer.token_address,
                        "token_symbol": transfer.token_symbol,
                        "amount": format(transfer.amount, "f"),
                        "classification": classification,
                        "classification_label": label,
                        "signals": list(signals),
                        "notified": notified,
                        "telegram_message_id": telegram_message_id,
                        "notify_error": notify_error,
                        "detection_latency_s": None if detection_latency_s is None else round(detection_latency_s, 3),
                        "processing_ms": round(processing_ms, 3),
                        "expected": None,
                        "confirmed": transfer.confirmed,
                        "error": analysis_error,
                    })
                    if user_state is not None:
                        try:
                            user_state.record_report_transfer(
                                event_key=audit_event_key(audit_record),
                                network=transfer.network,
                                wallet_address=subscription.wallet_address,
                                tx_hash=transfer.tx_hash,
                                direction=transfer.direction,
                                token_symbol=transfer.token_symbol,
                                token_name=transfer.token_name,
                                amount=format(transfer.amount, "f"),
                                classification=classification,
                                block_time=block_time.astimezone(timezone.utc).isoformat(),
                                notified=notified,
                            )
                        except Exception:
                            logger.warning("Rapor kaydı yazılamadı.")
                except Exception:
                    logger.exception(
                        "Transfer işlenemedi | ağ=%s | tx=%s",
                        transfer.network,
                        transfer.tx_hash,
                    )

        async def seed_address_history(network: str, wallet_address: str, transfers: list[TokenTransfer]) -> None:
            """Baseline transfers fill the counterparty history; they are not alerts."""
            if user_state is None:
                return
            seen = {
                transfer.sender if transfer.direction == "RECEIVE" else transfer.receiver
                for transfer in transfers
            }
            user_state.record_counterparties(network, wallet_address, sorted(seen))

        async def token_decimals(network: str, token_address: str) -> int | None:
            provider_for = providers.get(network)
            if provider_for is None:
                return None
            info = await provider_for.get_contract_info(token_address)
            return info.decimals

        async def handle_approval(subscription, event, assessment, risk) -> None:
            logger.info(
                "Approval olayı | durum=%s | skor=%d | seviye=%s",
                assessment.state, risk.score, risk.level,
            )
            if telegram is None:
                return
            recipient = subscription.user_id or settings.telegram_chat_id
            alias = None
            if subscription.user_id and user_state is not None:
                try:
                    prefs = user_state.settings(subscription.user_id, event.network, subscription.wallet_address)
                except Exception:
                    prefs = None
                if prefs is not None:
                    alias = prefs.alias or None
                    reason = skip_notification(
                        "", assessment.human_value or 0, prefs, datetime.now(timezone.utc),
                        extra_suspicious=risk.score >= 25, apply_min_amount=False,
                    )
                    if reason:
                        logger.info("Approval bildirimi atlandı | neden=%s", reason)
                        return
            if not recipient:
                return
            symbol = None
            try:
                info = await providers[event.network].get_contract_info(event.token_address)
                symbol = info.symbol
            except Exception:
                symbol = None
            try:
                await telegram.send_approval_alert(event, assessment, risk, chat_id=recipient, alias=alias, token_symbol=symbol)
                if operations: operations.increment("notifications_sent")
            except Exception as exc:
                if operations: operations.increment("errors")
                logger.error("Approval bildirimi gönderilemedi: %s", exc)

        async def handle_api_status(subscription, status: str) -> None:
            if telegram is None:
                return
            try:
                recipient = subscription.user_id or settings.telegram_chat_id
                if recipient:
                    await telegram.send_api_status_alert(status, chat_id=recipient)
            except TelegramError as exc:
                logger.error("API durumu bildirimi gönderilemedi: %s", exc)

        async def heartbeat() -> None:
            last_summary = time.monotonic()
            while True:
                await asyncio.sleep(30)
                try:
                    report = build_health_report(state=health_state, provider=providers, store=wallet_store,
                        operations=operations, version=bot_version, commit=get_git_commit(), data_dir=data_dir, log_dir=log_dir)
                    await asyncio.to_thread(write_heartbeat, data_dir, health_state, report["icon"])
                except Exception:
                    logger.exception("Heartbeat yerel dosyaya yazılamadı.")
                if time.monotonic() - last_summary < 600:
                    continue
                last_summary = time.monotonic()
                current_subscriptions = wallet_store.all_subscriptions()
                wallet_count = len({
                    (item.network, item.wallet_address)
                    for item in current_subscriptions
                })
                observed, errors = take_interval_counts()
                logger.info(
                    "Heartbeat | bot_version=%s | run_id=%s | izlenen_cuzdan=%d | gozlenen_transfer=%d | hata=%d | uptime_s=%.0f",
                    bot_version,
                    get_run_id(),
                    wallet_count,
                    observed,
                    errors,
                    time.monotonic() - started_at,
                )

        async def external_heartbeat() -> None:
            if not settings.heartbeat_url:
                return
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while True:
                    await asyncio.sleep(300)
                    try:
                        async with session.get(settings.heartbeat_url) as response:
                            await response.read()
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        logger.debug("Dış heartbeat isteği başarısız oldu.")

        monitor = WalletMonitorManager(
            client=providers,
            store=wallet_store,
            poll_interval=settings.poll_interval,
            max_poll_interval=settings.max_poll_interval,
            normalizer=normalize_etherscan_transfers,
            on_transfers=handle_new_transfers,
            on_api_status=handle_api_status,
            health_state=health_state,
            on_baseline=seed_address_history if user_state is not None else None,
        )
        approval_monitor = None
        if user_state is not None:
            approval_monitor = ApprovalMonitor(
                providers, wallet_store, user_state, handle_approval,
                interval=settings.approval_poll_interval, decimals_lookup=token_decimals,
            )
            supported = approval_monitor.supported_networks()
            if supported:
                logger.info("Approval izleme etkin | aglar=%s | aralik=%.0fs", ",".join(supported), approval_monitor.interval)
            else:
                approval_monitor = None
                logger.info("Approval izleme için destekli ağ yok.")

        telegram_bot = (
            TelegramBotController(
                bot_token=settings.telegram_bot_token,
                monitor=monitor,
                notifier=telegram,
                wallet_store=wallet_store,
                price_client=price_client,
                check_service=check_service,
                admin_ids=settings.admin_ids,
                operations_store=operations,
                health_reporter=lambda: build_health_report(
                    state=health_state, provider=providers, store=wallet_store,
                    operations=operations, version=bot_version, commit=get_git_commit(),
                    data_dir=data_dir, log_dir=log_dir,
                ),
                stats_reporter=lambda: build_stats_report(operations, providers),
                user_state=user_state,
            )
            if telegram is not None
            else None
        )

        initial_report = build_health_report(state=health_state, provider=providers, store=wallet_store,
            operations=operations, version=bot_version, commit=get_git_commit(), data_dir=data_dir, log_dir=log_dir)
        await asyncio.to_thread(write_heartbeat, data_dir, health_state, initial_report["icon"])
        weekly_reporter = WeeklyReporter(user_state, wallet_store, telegram) if user_state is not None and telegram is not None else None
        tasks = [asyncio.create_task(monitor.run()), asyncio.create_task(heartbeat())]
        if weekly_reporter is not None:
            tasks.append(asyncio.create_task(weekly_reporter.run()))
        if approval_monitor is not None:
            tasks.append(asyncio.create_task(approval_monitor.run()))
        if settings.heartbeat_url:
            tasks.append(asyncio.create_task(external_heartbeat()))
        if telegram_bot is not None:
            tasks.append(asyncio.create_task(telegram_bot.run()))

        stop_task = asyncio.create_task(stop_event.wait())
        try:
            done, _ = await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
            if stop_task not in done:
                for completed in done:
                    completed.result()
                raise RuntimeError("Bir bot görevi beklenmedik biçimde durdu.")
            clean_shutdown = True
            logger.info("Kapanış sinyali alındı; görevler kontrollü durduruluyor.")
        finally:
            for task in tasks:
                task.cancel()
            stop_task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, stop_task, return_exceptions=True), timeout=12)
            except asyncio.TimeoutError:
                logger.error("Kapanış görevleri 20 saniyede tamamlanmadı; takılan görevler iptal edildi.")
                for task in tasks: task.cancel()
            if telegram is not None:
                try:
                    await asyncio.wait_for(asyncio.gather(*(
                        telegram.send_text("🔴 Bot durduruldu", chat_id=admin_id)
                        for admin_id in settings.admin_ids
                    ), return_exceptions=True), timeout=3)
                except asyncio.TimeoutError:
                    logger.warning("Kapanış bildirimi zaman aşımına uğradı.")
            await asyncio.gather(*(item.close() for item in providers.values()), return_exceptions=True)
            await price_client.close()
            if telegram_bot is not None:
                await telegram_bot.close()
            if telegram is not None:
                await telegram.close()
            if user_state is not None:
                user_state.close()
    except Exception:
        logger.exception("Bot beklenmeyen bir hatayla durdu.")
        raise
    finally:
        logger.info(
            "Bot durduruldu | run_id=%s | kesintisiz_calisma_s=%d",
            get_run_id(),
            int(time.monotonic() - started_at),
        )
        if clean_shutdown and operations is not None and run_session_id is not None:
            try: operations.end_session(run_session_id)
            except Exception: logger.exception("Çalışma oturumu temiz kapatılırken SQLite yazımı başarısız oldu.")
        flush_logging()
        shutdown_logging()
        for sig in installed_signals:
            try: loop.remove_signal_handler(sig)
            except Exception: pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
