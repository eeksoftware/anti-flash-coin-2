"""Check configured Etherscan keys with one harmless V2 block-number call each."""
import asyncio

from blockchain.etherscan import EtherscanClient, EtherscanError
from config import settings


async def main() -> int:
    client = EtherscanClient()
    try:
        for index, secret in enumerate(settings.etherscan_api_keys, 1):
            label = f"key-{index}"
            # Make each key explicit without ever printing its value.
            try:
                await client.start()
                assert client.session is not None
                async with client.session.get(client.BASE_URL, params={
                    "chainid": "1", "module": "proxy", "action": "eth_blockNumber",
                    "apikey": secret.reveal(),
                }) as response:
                    payload = await response.json()
                valid = isinstance(payload, dict) and payload.get("status") != "0" and bool(payload.get("result"))
                print(f"{label}: {'valid' if valid else 'invalid'}")
            except Exception as exc:
                print(f"{label}: request failed ({type(exc).__name__})")
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
