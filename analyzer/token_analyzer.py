import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from models.transaction import TokenTransfer
from networks import get_network_config


TokenClassification = Literal["CANONICAL", "VARIANT", "KNOWN_FRAUD", "UNKNOWN"]
logger = logging.getLogger(__name__)
DETAIL_TRANSLATIONS = {
    "bridged": "köprülenmiş varlık",
    "non_canonical": "kanonik olmayan sözleşme",
    "legacy": "eski sürüm",
    "deprecated": "kullanımdan kaldırılmış",
    "wound_down": "faaliyeti sonlandırılmış",
    "transitioning": "geçiş sürecinde",
    "token_impersonation": "başka bir tokenin taklidi",
    "suspicious_token": "şüpheli token",
}


@dataclass(frozen=True)
class TokenAnalysis:
    classification: TokenClassification
    matched_token: str | None = None
    details: str | None = None


class TokenAnalyzer:
    def __init__(self, registry_dir: Path | None = None) -> None:
        if registry_dir is None:
            registry_dir = Path(__file__).resolve().parents[1] / "registry"

        self.tokens = self._load_json(registry_dir / "tokens.json")
        self.variants = self._load_json(registry_dir / "known_token_variants.json")
        self.fraud = self._load_json(registry_dir / "known_fraud_tokens.json")
        self._warn_about_incomplete_ethereum_fraud_addresses()

    @staticmethod
    def _load_json(path: Path) -> dict:
        with path.open(encoding="utf-8") as registry_file:
            data = json.load(registry_file)
        if not isinstance(data, dict):
            raise ValueError(f"Kayıt dosyası JSON nesnesi biçiminde olmalı: {path}")
        return data

    def analyze(self, transfer: TokenTransfer) -> TokenAnalysis:
        return self.analyze_token(
            transfer.network,
            transfer.token_address,
            transfer.token_symbol,
        )

    def analyze_token(
        self,
        network_name: str,
        token_address: str,
        token_symbol: str = "",
    ) -> TokenAnalysis:
        """Analyze a token address using the exact same registry rules as transfers."""
        config = get_network_config(network_name)
        network = config.name
        contract = config.normalize_address(token_address)

        # Match only exact network + contract deployments. Names and symbols
        # are deliberately not identity checks.
        for symbol, token in self.tokens.get("tokens", {}).items():
            deployment = token.get("networks", {}).get(network, {})
            registered_contract = deployment.get("contract")
            if isinstance(registered_contract, str) and config.normalize_address(registered_contract) == contract:
                return TokenAnalysis("CANONICAL", symbol)

        for symbol, networks in self.variants.items():
            for variant in networks.get(network, []):
                registered_contract = variant.get("contract")
                if isinstance(registered_contract, str) and config.normalize_address(registered_contract) == contract:
                    detail = variant.get("classification") or variant.get("status")
                    if isinstance(detail, str):
                        detail = DETAIL_TRANSLATIONS.get(detail.lower(), detail)
                    return TokenAnalysis("VARIANT", symbol, detail)

        for token in self.fraud.get("known_fraud_tokens", []):
            registered_contract = token.get("contract")
            if (
                token.get("network", "").lower() == network
                and isinstance(registered_contract, str)
                and config.normalize_address(registered_contract) == contract
            ):
                return TokenAnalysis(
                    "KNOWN_FRAUD",
                    token.get("impersonates") or token_symbol,
                    token.get("reason"),
                )

        return TokenAnalysis(
            "UNKNOWN",
            details="Token sistemimizde kayıtlı değil; bu durum tek başına sahte olduğunu göstermez. Lütfen sizde manuel olarak kontrol ediniz.",
        )

    def _warn_about_incomplete_ethereum_fraud_addresses(self) -> None:
        for token in self.fraud.get("known_fraud_tokens", []):
            contract = token.get("contract")
            if (
                token.get("network", "").lower() == "ethereum"
                and (
                    not isinstance(contract, str)
                    or not get_network_config("ethereum").is_valid_address(contract)
                )
            ):
                logger.warning(
                    "Dolandırıcılık listesindeki Ethereum kaydında tam sözleşme adresi yok; eşleştirilemedi: %s",
                    contract,
                )
