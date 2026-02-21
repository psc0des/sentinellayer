"""Microsoft Foundry client wrapper — GPT-4.1 calls for governance reasoning.

The Foundry account and GPT-4.1 model deployment are Terraform-managed
(azurerm_ai_services + azurerm_cognitive_deployment in infrastructure/terraform/).
After `terraform apply`, run `bash scripts/setup_env.sh` to auto-populate .env
with the endpoint and key from Terraform outputs.

Required env vars (written automatically by setup_env.sh):
    AZURE_OPENAI_ENDPOINT   — from Terraform output: foundry_endpoint
    AZURE_OPENAI_API_KEY    — from Terraform output: foundry_primary_key
    AZURE_OPENAI_DEPLOYMENT — from Terraform output: foundry_deployment (default "gpt-41")
    AZURE_OPENAI_API_VERSION — 2025-01-01-preview

Mode selection
--------------
Mock mode (USE_LOCAL_MOCKS=true, or endpoint not set):
    Returns a canned string without making any network call.
    This preserves the fully-offline behavior used in development.

Live mode (USE_LOCAL_MOCKS=false + AZURE_OPENAI_ENDPOINT set):
    Calls GPT-4.1 on Microsoft Foundry and returns the real reasoning text.

Usage::

    from src.infrastructure.openai_client import AzureOpenAIClient

    client = AzureOpenAIClient()
    reasoning = client.complete(
        system_prompt="You are a cloud infrastructure risk assessor.",
        user_message="Why is deleting a DR VM dangerous?",
    )
    print(reasoning)
"""

import logging

from src.config import settings as _default_settings

logger = logging.getLogger(__name__)


class AzureOpenAIClient:
    """Wrapper around Azure OpenAI chat completions with mock fallback.

    The ``is_mock`` property tells callers which mode is active so they
    can log a warning if they expected real LLM output.

    Args:
        cfg: Settings object.  Defaults to the module-level singleton
            from ``src.config``.  Pass a custom object in tests.
    """

    def __init__(self, cfg=None) -> None:
        self._cfg = cfg or _default_settings

        # Enter mock mode if the flag is set OR credentials are absent.
        # This means: even if USE_LOCAL_MOCKS=false, a missing endpoint
        # gracefully falls back to mock instead of crashing at import time.
        self._is_mock: bool = (
            self._cfg.use_local_mocks or not self._cfg.azure_openai_endpoint
        )

        if self._is_mock:
            logger.info("AzureOpenAIClient: LOCAL MOCK mode (no Foundry call).")
            self._client = None
        else:
            # Import lazily so the openai package is not required in mock mode
            from openai import AzureOpenAI  # type: ignore[import]

            self._client = AzureOpenAI(
                azure_endpoint=self._cfg.azure_openai_endpoint,
                api_key=self._cfg.azure_openai_api_key,
                api_version=self._cfg.azure_openai_api_version,
            )
            logger.info(
                "AzureOpenAIClient: connected to Microsoft Foundry at %s (deployment=%s)",
                self._cfg.azure_openai_endpoint,
                self._cfg.azure_openai_deployment,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 500,
        temperature: float = 0.1,
    ) -> str:
        """Send a chat completion request and return the response text.

        In mock mode, returns a static placeholder string instantly
        (no network call, no latency).

        Args:
            system_prompt: The LLM's role instruction (e.g. "You are a
                cloud risk assessor. Be concise and technical.").
            user_message: The specific question or context to reason about.
            max_tokens: Maximum tokens in the response (default 500).
            temperature: Sampling randomness — 0.0 is fully deterministic,
                1.0 is creative.  Default 0.1 (near-deterministic) for
                consistent governance reasoning.

        Returns:
            The LLM's response as a plain string.  Never raises on network
            errors — logs a warning and returns a fallback string instead.
        """
        if self._is_mock:
            return self._mock_response()

        try:
            response = self._client.chat.completions.create(
                model=self._cfg.azure_openai_deployment,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return response.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("AzureOpenAIClient: completion failed (%s) — using fallback.", exc)
            return self._mock_response()

    @property
    def is_mock(self) -> bool:
        """True if this client is running in local mock mode."""
        return self._is_mock

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mock_response() -> str:
        """Canned response for offline / test use."""
        return (
            "[MOCK] LLM reasoning not available in local mock mode. "
            "Set USE_LOCAL_MOCKS=false and provide Azure AI Foundry credentials "
            "to enable AI-powered governance reasoning."
        )
