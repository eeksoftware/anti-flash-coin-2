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
import logging
import time
from datetime import datetime, timezone

from audit_log import (
    CLASSIFICATION_LABELS,
    configure_logging,
    flush_logging,
    get_run_id,
    log_transaction,
    set_bot_version,
    shutdown_logging,
    take_interval_counts,
)
from analyzer.token_analyzer import TokenAnalyzer
from blockchain.etherscan import EtherscanClient
from blockchain.normalizer import normalize_etherscan_transfers
from blockchain.token_price import DexScreenerPriceClient
from config import settings
from models.transaction import TokenTransfer
from monitor.wallet_monitor import WalletMonitorManager
from monitor.wallet_store import WalletStateStore
from notifications.telegram import (
    TelegramBotController,
    TelegramError,
    TelegramNotifier,
)
from providers.ethereum import EthereumProvider
from services.check_service import CheckService
from version import get_git_commit, get_version


async def main() -> None:
    configure_logging(
        settings.log_dir,
        settings.log_level,
        settings.log_retention_days,
    )
    logger = logging.getLogger(__name__)
    started_at = time.monotonic()
    bot_version = get_version()
    set_bot_version(bot_version)
    logger.info(
        "Bot başlatıldı | sürüm=%s | git_commit=%s | run_id=%s",
        bot_version,
        get_git_commit(),
        get_run_id(),
    )

    try:
        if settings.network != "ethereum":
            raise ValueError(
                f"Mevcut sağlayıcı bu ağı desteklemiyor: {settings.network}"
            )

        wallet_store = WalletStateStore()
        wallet_store.ensure_legacy_wallet(settings.network, settings.monitored_wallet)

        subscriptions = wallet_store.all_subscriptions()
        logger.info(
            "İzlenen cüzdan sayısı: %d",
            len({(item.network, item.wallet_address.lower()) for item in subscriptions}),
        )

        client = EtherscanClient()
        provider = EthereumProvider(client)
        price_client = DexScreenerPriceClient()
        token_analyzer = TokenAnalyzer()
        check_service = CheckService({"ethereum": provider}, token_analyzer)
        telegram = None
        if settings.telegram_bot_token:
            telegram = TelegramNotifier(
                settings.telegram_bot_token,
                settings.telegram_chat_id,
            )
        else:
            logger.info("Telegram bildirimleri kapalı; bot bilgileri yapılandırılmamış.")

        async def handle_new_transfers(subscription, transfers: list[TokenTransfer]) -> None:
            for transfer in transfers:
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
                label = CLASSIFICATION_LABELS.get(classification, "Analiz hatası")
                logger.info(
                    "%s %s %s olarak sınıflandırıldı: %s",
                    "Alındı" if transfer.direction == "RECEIVE" else "Gönderildi",
                    transfer.amount,
                    transfer.token_symbol,
                    label,
                )

                notified = None
                telegram_message_id = None
                notify_error = None
                if analysis is not None and telegram is not None:
                    recipient = subscription.user_id or settings.telegram_chat_id
                    if recipient:
                        try:
                            result = await telegram.send_transfer_alert(
                                transfer,
                                analysis,
                                chat_id=recipient,
                            )
                            notified = True
                            if isinstance(result, dict):
                                telegram_message_id = result.get("message_id")
                        except Exception as exc:
                            notified = False
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
                signals = getattr(analysis, "signals", []) if analysis is not None else []
                if not isinstance(signals, (list, tuple)):
                    signals = []
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
                    "detection_latency_s": round(detection_latency_s, 3),
                    "processing_ms": round(processing_ms, 3),
                    "expected": None,
                    "error": analysis_error,
                })

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
            while True:
                await asyncio.sleep(600)
                current_subscriptions = wallet_store.all_subscriptions()
                wallet_count = len({
                    (item.network, item.wallet_address.lower())
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

        monitor = WalletMonitorManager(
            client=provider,
            store=wallet_store,
            poll_interval=settings.poll_interval,
            max_poll_interval=settings.max_poll_interval,
            normalizer=normalize_etherscan_transfers,
            on_transfers=handle_new_transfers,
            on_api_status=handle_api_status,
        )

        telegram_bot = (
            TelegramBotController(
                bot_token=settings.telegram_bot_token,
                monitor=monitor,
                notifier=telegram,
                wallet_store=wallet_store,
                price_client=price_client,
                check_service=check_service,
            )
            if telegram is not None
            else None
        )

        tasks = [asyncio.create_task(monitor.run()), asyncio.create_task(heartbeat())]
        if telegram_bot is not None:
            tasks.append(asyncio.create_task(telegram_bot.run()))

        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await provider.close()
            await price_client.close()
            if telegram is not None:
                await telegram.close()
    except Exception:
        logger.exception("Bot beklenmeyen bir hatayla durdu.")
        raise
    finally:
        logger.info(
            "Bot durduruldu | run_id=%s | kesintisiz_calisma_s=%d",
            get_run_id(),
            int(time.monotonic() - started_at),
        )
        flush_logging()
        shutdown_logging()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
