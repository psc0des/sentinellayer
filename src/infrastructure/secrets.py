"""Key Vault secret resolution helpers for Azure clients.

Resolution order for each secret:
1) Direct env value (for local/dev overrides)
2) Azure Key Vault secret (via DefaultAzureCredential)
3) Empty string (caller decides fallback behavior)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class KeyVaultSecretResolver:
    """Resolves secrets from direct values or Azure Key Vault."""

    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self._client = None
        self._init_attempted = False
        self._cache: dict[str, str] = {}

    def resolve(
        self,
        direct_value: str,
        secret_name: str,
        setting_name: str,
    ) -> str:
        """Return a secret value from env override or Key Vault."""
        if direct_value:
            return direct_value

        if not secret_name:
            return ""

        if secret_name in self._cache:
            return self._cache[secret_name]

        client = self._get_client()
        if client is None:
            return ""

        try:
            secret = client.get_secret(secret_name)
            value = secret.value or ""
            if value:
                self._cache[secret_name] = value
                logger.info(
                    "KeyVaultSecretResolver: loaded %s from Key Vault secret '%s'.",
                    setting_name,
                    secret_name,
                )
            return value
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "KeyVaultSecretResolver: failed to read secret '%s' for %s (%s).",
                secret_name,
                setting_name,
                exc,
            )
            return ""

    def _get_client(self):
        if self._init_attempted:
            return self._client

        self._init_attempted = True

        vault_url = getattr(self._cfg, "azure_keyvault_url", "")
        if not vault_url:
            return None

        try:
            from azure.identity import DefaultAzureCredential  # type: ignore[import]
            from azure.keyvault.secrets import SecretClient  # type: ignore[import]
        except ImportError:
            logger.warning(
                "KeyVaultSecretResolver: azure-identity/azure-keyvault-secrets not installed."
            )
            return None

        managed_identity_client_id = (
            getattr(self._cfg, "azure_managed_identity_client_id", "") or None
        )

        try:
            credential = DefaultAzureCredential(
                managed_identity_client_id=managed_identity_client_id
            )
            self._client = SecretClient(vault_url=vault_url, credential=credential)
            logger.info("KeyVaultSecretResolver: using Key Vault %s", vault_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "KeyVaultSecretResolver: failed to initialize Key Vault client (%s).",
                exc,
            )
            self._client = None

        return self._client
