"""Manually inspect and audit a recent ERC-20 transfer by transaction hash."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
if sys.prefix == sys.base_prefix and PROJECT_PYTHON.is_file():
    os.execv(str(PROJECT_PYTHON), [str(PROJECT_PYTHON), *sys.argv])
sys.path.insert(0, str(PROJECT_ROOT))

from analyzer.token_analyzer import TokenAnalyzer
from audit_log import (
    CLASSIFICATION_LABELS,
    configure_logging,
    flush_logging,
    log_transaction,
    shutdown_logging,
)
from blockchain.etherscan import EtherscanClient
from blockchain.normalizer import normalize_etherscan_transfers
from config import settings
from networks import get_network_config
from notifications.telegram_formatter import sanitize_token_text


ETHEREUM = get_network_config("ethereum")
ADDRESS_RE = ETHEREUM.address_regex
TX_HASH_RE = ETHEREUM.tx_hash_regex
PAGE_SIZE = 100
MAX_PAGES = 100


async def check_transaction(args: argparse.Namespace) -> int:
    if settings.network != "ethereum" or args.network != "ethereum":
        raise ValueError("Manuel hash kontrolü şu anda yalnızca Ethereum ağında destekleniyor.")
    if not ADDRESS_RE.fullmatch(args.wallet):
        raise ValueError("--wallet geçerli bir Ethereum adresi olmalı.")
    if not TX_HASH_RE.fullmatch(args.tx_hash):
        raise ValueError("--tx-hash geçerli bir 0x işlem hash'i olmalı.")

    logger = logging.getLogger(__name__)
    client = EtherscanClient()
    analyzer = TokenAnalyzer()
    try:
        matched = []
        scanned_pages = 0
        for page in range(1, MAX_PAGES + 1):
            scanned_pages = page
            raw = await client.get_token_transfers(
                args.wallet,
                page=page,
                offset=PAGE_SIZE,
            )
            transfers = normalize_etherscan_transfers(raw, args.wallet)
            matched.extend(
                transfer for transfer in transfers
                if transfer.tx_hash.lower() == args.tx_hash.lower()
            )
            if matched or len(raw) < PAGE_SIZE:
                break

        if not matched:
            logger.warning(
                "İşlem bulunamadı | tx_hash=%s | taranan_sayfa=%d",
                args.tx_hash,
                scanned_pages,
            )
            return 1

        for transfer in matched:
            started = time.perf_counter()
            analysis_error = None
            try:
                analysis = analyzer.analyze(transfer)
            except Exception as exc:
                analysis = None
                analysis_error = str(exc)
                logging.getLogger(__name__).exception("Manuel transfer analizi başarısız oldu.")
            processing_ms = (time.perf_counter() - started) * 1000
            checked_at = datetime.now(timezone.utc)
            block_time = transfer.timestamp
            if block_time.tzinfo is None:
                block_time = block_time.replace(tzinfo=timezone.utc)
            latency = max(
                0.0,
                (checked_at - block_time.astimezone(timezone.utc)).total_seconds(),
            )
            classification = analysis.classification if analysis is not None else "ERROR"
            label = CLASSIFICATION_LABELS.get(classification, "Analiz hatası")
            detected = classification != "CANONICAL"
            log_transaction({
                "ts": checked_at,
                "source": "manual",
                "network": transfer.network,
                "tx_hash": transfer.tx_hash,
                "log_index": transfer.log_index,
                "event_key": transfer.event_key,
                "block_number": transfer.block_number,
                "block_time": block_time,
                "direction": transfer.direction,
                "wallet": args.wallet.lower(),
                "sender": transfer.sender,
                "receiver": transfer.receiver,
                "token_address": transfer.token_address,
                "token_symbol": transfer.token_symbol,
                "amount": format(transfer.amount, "f"),
                "classification": classification,
                "classification_label": label,
                "signals": getattr(analysis, "signals", []) if analysis is not None else [],
                "notified": None,
                "detection_latency_s": round(latency, 3) if detected else None,
                "processing_ms": round(processing_ms, 3),
                "expected": args.expected,
                "note": args.note,
                "error": analysis_error,
            })
            print(f"{transfer.tx_hash} · {label} · {sanitize_token_text(transfer.token_symbol)} · {transfer.amount}")
        return 0
    finally:
        await client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ERC-20 transferi için manuel sınıflandırma kaydı oluştur.")
    parser.add_argument("--wallet", required=True, help="İzlenen Ethereum cüzdan adresi")
    parser.add_argument("--tx-hash", required=True, help="Kontrol edilecek 0x işlem hash'i")
    parser.add_argument("--network", default="ethereum", choices=("ethereum",))
    parser.add_argument("--expected", choices=("gercek", "sahte", "supheli"))
    parser.add_argument("--note", help="Tez değerlendirmesi için kısa not")
    return parser.parse_args()


def main() -> int:
    configure_logging(settings.log_dir, settings.log_level, settings.log_retention_days)
    try:
        return asyncio.run(check_transaction(parse_args()))
    except Exception:
        logging.getLogger(__name__).exception("Manuel işlem kontrolü başarısız oldu.")
        return 1
    finally:
        flush_logging()
        shutdown_logging()


if __name__ == "__main__":
    raise SystemExit(main())
