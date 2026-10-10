"""Check an Ethereum token contract or transaction from the command line."""

from __future__ import annotations

import argparse
import asyncio
import html
import os
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
if sys.prefix == sys.base_prefix and PROJECT_PYTHON.is_file():
    os.execv(str(PROJECT_PYTHON), [str(PROJECT_PYTHON), "-m", "tools.check", *sys.argv[1:]])
os.environ.setdefault("SSL_CERT_FILE", "/etc/ssl/cert.pem")
sys.path.insert(0, str(PROJECT_ROOT))

from analyzer.token_analyzer import TokenAnalyzer
from audit_log import configure_logging, flush_logging, shutdown_logging
from blockchain.etherscan import EtherscanClient, EtherscanError, EtherscanRateLimitError, EtherscanTimeoutError
from notifications.telegram_formatter import format_token_check, format_tx_check
from providers.ethereum import EthereumProvider
from services.check_input import InvalidCheckInput, parse_check_args
from services.check_service import CheckService


def _plain_text(message: str) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", message))


async def _run(args: argparse.Namespace) -> int:
    try:
        request = parse_check_args([args.network, args.value])
    except (InvalidCheckInput, ValueError):
        print("Geçersiz ağ veya değer. Kullanım: python -m tools.check <ağ> <değer>", file=sys.stderr)
        return 2

    client = EtherscanClient()
    provider = EthereumProvider(client)
    service = CheckService({"ethereum": provider}, TokenAnalyzer())
    try:
        if request.kind == "token":
            result = await service.check_token(
                request.network, request.value, expected=args.expected, note=args.note
            )
            message = format_token_check(result)
        else:
            result = await service.check_transaction(
                request.network, request.value, expected=args.expected, note=args.note
            )
            message = format_tx_check(result)
            if result.transaction is None:
                print(_plain_text(message.text))
                return 1
        print(_plain_text(message.text))
        return 0
    except EtherscanRateLimitError:
        print("Etherscan istek sınırına ulaşıldı. Daha sonra yeniden dene.", file=sys.stderr)
        return 1
    except (EtherscanTimeoutError, EtherscanError):
        print("Blokzincir sağlayıcısından yanıt alınamadı.", file=sys.stderr)
        return 1
    except Exception as exc:
        # Avoid printing provider exception text, which may contain request data.
        print(f"Kontrol tamamlanamadı ({type(exc).__name__}).", file=sys.stderr)
        return 1
    finally:
        await provider.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Token kontratı veya işlem kontrolü")
    parser.add_argument("network", help="Ağ adı; şu anda ethereum")
    parser.add_argument("value", help="0x token kontratı veya işlem hash'i")
    parser.add_argument("--expected", choices=("gercek", "sahte", "supheli"))
    parser.add_argument("--note", help="Değerlendirme notu")
    args = parser.parse_args()
    configure_logging()
    try:
        return asyncio.run(_run(args))
    finally:
        flush_logging()
        shutdown_logging()


if __name__ == "__main__":
    raise SystemExit(main())
