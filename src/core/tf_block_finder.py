"""tf_block_finder.py — Find Terraform resource blocks matching an Azure resource.

Given a list of .tf file contents and an Azure resource name, this module
locates the Terraform resource block that manages that Azure resource.

Three deterministic passes are tried in order:
  1. Exact name match     — name = "azure-resource-name"
  2. Static prefix match  — handles "${var.suffix}" interpolation by checking
                            that all static segments of the TF name appear
                            in order within the Azure resource name.
  3. tfvars resolution    — reads terraform.tfvars / variable defaults to
                            resolve variable values, then exact-matches again.

If all three fail, an async LLM fallback is available: it sends candidate
block name values to the model and asks which one matches.

A second async function (resolve_config_change_with_llm) is provided for
UPDATE_CONFIG actions where we need to determine which Terraform attribute
to change and to what value.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# =============================================================================
# ARM resource type  →  Terraform resource type(s)
# Keys are lowercase ARM resource type strings (from ActionTarget.resource_type).
# Values are ordered lists — most common / modern provider type first.
# =============================================================================
ARM_TO_TF_TYPE: dict[str, list[str]] = {
    "microsoft.web/serverfarms": [
        "azurerm_service_plan",
        "azurerm_app_service_plan",
    ],
    "microsoft.web/sites": [
        "azurerm_linux_web_app",
        "azurerm_windows_web_app",
        "azurerm_app_service",
    ],
    "microsoft.compute/virtualmachines": [
        "azurerm_linux_virtual_machine",
        "azurerm_windows_virtual_machine",
        "azurerm_virtual_machine",
    ],
    "microsoft.compute/virtualmachinescalesets": [
        "azurerm_linux_virtual_machine_scale_set",
        "azurerm_windows_virtual_machine_scale_set",
        "azurerm_orchestrated_virtual_machine_scale_set",
    ],
    "microsoft.network/networksecuritygroups": [
        "azurerm_network_security_group",
    ],
    "microsoft.network/networksecurityrules": [
        "azurerm_network_security_rule",
    ],
    "microsoft.storage/storageaccounts": [
        "azurerm_storage_account",
    ],
    "microsoft.sql/servers": [
        "azurerm_mssql_server",
        "azurerm_sql_server",
    ],
    "microsoft.sql/servers/databases": [
        "azurerm_mssql_database",
        "azurerm_sql_database",
    ],
    "microsoft.keyvault/vaults": [
        "azurerm_key_vault",
    ],
    "microsoft.containerservice/managedclusters": [
        "azurerm_kubernetes_cluster",
    ],
    "microsoft.containerregistry/registries": [
        "azurerm_container_registry",
    ],
    "microsoft.app/containerapps": [
        "azurerm_container_app",
    ],
    "microsoft.app/managedenvironments": [
        "azurerm_container_app_environment",
    ],
    "microsoft.operationalinsights/workspaces": [
        "azurerm_log_analytics_workspace",
    ],
    "microsoft.insights/components": [
        "azurerm_application_insights",
    ],
    "microsoft.network/virtualnetworks": [
        "azurerm_virtual_network",
    ],
    "microsoft.network/publicipaddresses": [
        "azurerm_public_ip",
    ],
    "microsoft.network/loadbalancers": [
        "azurerm_lb",
    ],
    "microsoft.recoveryservices/vaults": [
        "azurerm_recovery_services_vault",
    ],
    "microsoft.resources/resourcegroups": [
        "azurerm_resource_group",
    ],
}

# =============================================================================
# Terraform resource type  →  its SKU / size attribute name
# Only simple top-level string attributes are listed here.
# Types where the SKU lives inside a nested block (e.g. azurerm_app_service_plan
# which uses `sku { tier = ... size = ... }`) are intentionally omitted —
# the LLM path handles those because a regex patch isn't safe for nested blocks.
# =============================================================================
TF_SKU_ATTRIBUTE: dict[str, str] = {
    "azurerm_service_plan": "sku_name",
    "azurerm_linux_virtual_machine": "size",
    "azurerm_windows_virtual_machine": "size",
    "azurerm_virtual_machine": "vm_size",
    "azurerm_mssql_database": "sku_name",
    "azurerm_sql_database": "edition",
    "azurerm_storage_account": "account_tier",
    "azurerm_key_vault": "sku_name",
    "azurerm_container_registry": "sku",
}


@dataclass
class BlockMatch:
    """A located Terraform resource block."""

    tf_type: str             # e.g. "azurerm_service_plan"
    logical_name: str        # e.g. "prod"
    file_path: str           # e.g. "infrastructure/terraform-demo/main.tf"
    file_sha: str            # git blob SHA (needed for repo.update_file)
    start_line: int          # 0-indexed line where `resource "..." "..." {` appears
    end_line: int            # 0-indexed line of the closing `}`
    raw_block: str           # full block text (for preview / LLM context)
    name_value: str          # raw value of the `name` field (may contain interpolation)
    current_attr_value: Optional[str] = None  # current value of the SKU/config attribute

    @property
    def address(self) -> str:
        """Terraform resource address, e.g. 'azurerm_service_plan.prod'."""
        return f"{self.tf_type}.{self.logical_name}"


@dataclass
class TfFile:
    """A fetched .tf file with its path, git SHA, and decoded content."""

    file_path: str
    file_sha: str
    content: str


# =============================================================================
# Internal helpers
# =============================================================================

def _extract_blocks(content: str, tf_type: str) -> list[dict]:
    """Extract all resource blocks of tf_type from .tf file content.

    Returns a list of dicts with keys:
      logical_name, start_line, end_line, lines, fields
    where ``fields`` is a dict of top-level ``key = "value"`` pairs.
    """
    lines = content.split("\n")
    blocks: list[dict] = []

    # Matches: resource "TYPE" "LOGICAL_NAME" {
    # The opening brace may be on the same line or the next line.
    resource_pattern = re.compile(
        r'^\s*resource\s+"' + re.escape(tf_type) + r'"\s+"(\w+)"\s*\{?\s*$'
    )

    i = 0
    while i < len(lines):
        m = resource_pattern.match(lines[i])
        if m:
            logical_name = m.group(1)
            start_line = i
            brace_depth = lines[i].count("{") - lines[i].count("}")

            # If the opening brace is on the next line, step forward
            if brace_depth == 0 and i + 1 < len(lines):
                i += 1
                brace_depth += lines[i].count("{") - lines[i].count("}")

            block_lines = [lines[start_line]] if start_line == i else [lines[start_line], lines[i]]
            fields: dict[str, str] = {}

            while i + 1 < len(lines) and brace_depth > 0:
                i += 1
                line = lines[i]
                block_lines.append(line)
                brace_depth += line.count("{") - line.count("}")

                # Capture key = "value" at depth 1 (top-level inside the block)
                if brace_depth == 1:
                    kv = re.match(r'\s*(\w+)\s*=\s*"([^"]*)"', line)
                    if kv:
                        fields[kv.group(1)] = kv.group(2)

            blocks.append({
                "logical_name": logical_name,
                "start_line": start_line,
                "end_line": i,
                "lines": block_lines,
                "fields": fields,
            })
        i += 1

    return blocks


def _name_matches_exact(name_value: str, azure_name: str) -> bool:
    """Case-insensitive exact match between TF name field and Azure resource name."""
    return name_value.lower() == azure_name.lower()


def _name_matches_interpolated(name_value: str, azure_name: str) -> bool:
    """Check whether azure_name could be produced by name_value with var substitutions.

    Strips all ``${...}`` interpolation expressions from name_value, leaving
    only the static character segments.  If every static segment appears in
    azure_name in order (as a subsequence of substrings), this is a match.

    Examples
    --------
    "asp-ruriskry-prod-${var.suffix}"  → static: ["asp-ruriskry-prod-"]
    azure_name "asp-ruriskry-prod-demo" starts with it → match ✓

    "${var.prefix}-prod-demo"  → static: ["-prod-demo"]
    azure_name "myprefix-prod-demo" contains it → match ✓

    "${var.env}-app-${var.suffix}"  → static: ["-app-"]
    azure_name "prod-app-v2" contains it → match ✓
    """
    if not name_value or not azure_name:
        return False
    if "${" not in name_value:
        return False  # no interpolation — handled by exact match

    az = azure_name.lower()
    segments = [s for s in re.split(r"\$\{[^}]+\}", name_value.lower()) if s]

    if not segments:
        return False  # entirely interpolated — cannot determine match

    pos = 0
    for segment in segments:
        idx = az.find(segment, pos)
        if idx == -1:
            return False
        pos = idx + len(segment)
    return True


def _parse_tfvars(tf_files: list[TfFile]) -> dict[str, str]:
    """Build a vars dict from terraform.tfvars and variable default values.

    Pass 1: ``variable "name" { default = "value" }`` blocks in any .tf file.
    Pass 2: ``key = "value"`` pairs in any .tfvars file (overrides defaults).
    """
    vars_dict: dict[str, str] = {}

    # Pass 1: variable defaults from .tf files
    for tf_file in tf_files:
        for m in re.finditer(
            r'variable\s+"(\w+)"\s*\{[^}]*default\s*=\s*"([^"]*)"',
            tf_file.content, re.DOTALL,
        ):
            vars_dict[m.group(1)] = m.group(2)

    # Pass 2: explicit .tfvars values override defaults
    for tf_file in tf_files:
        if tf_file.file_path.endswith((".tfvars", "terraform.tfvars")):
            for m in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', tf_file.content):
                vars_dict[m.group(1)] = m.group(2)

    return vars_dict


def _resolve_interpolation(name_value: str, vars_dict: dict[str, str]) -> str:
    """Replace ``${var.X}`` and ``${local.X}`` with known values from vars_dict."""
    def replacer(m: re.Match) -> str:
        expr = m.group(1)               # e.g. "var.suffix" or "local.prefix"
        parts = expr.split(".", 1)
        if len(parts) == 2 and parts[0] in ("var", "local"):
            return vars_dict.get(parts[1], m.group(0))
        return m.group(0)

    return re.sub(r"\$\{([^}]+)\}", replacer, name_value)


def _scan_files_for_match(
    tf_files: list[TfFile],
    tf_types: list[str],
    azure_name: str,
    pass_name: str,
    match_fn,
) -> Optional[BlockMatch]:
    """Shared scanning loop used by all deterministic passes."""
    for tf_file in tf_files:
        for tf_type in tf_types:
            blocks = _extract_blocks(tf_file.content, tf_type)
            for block in blocks:
                raw_name = block["fields"].get("name", "")
                if raw_name and match_fn(raw_name, azure_name):
                    logger.info(
                        "tf_block_finder [%s]: found %s.%s (name=%r) in %s",
                        pass_name, tf_type, block["logical_name"],
                        raw_name, tf_file.file_path,
                    )
                    return BlockMatch(
                        tf_type=tf_type,
                        logical_name=block["logical_name"],
                        file_path=tf_file.file_path,
                        file_sha=tf_file.file_sha,
                        start_line=block["start_line"],
                        end_line=block["end_line"],
                        raw_block="\n".join(block["lines"]),
                        name_value=raw_name,
                    )
    return None


# =============================================================================
# Public synchronous API
# =============================================================================

def find_tf_block(
    tf_files: list[TfFile],
    azure_name: str,
    tf_types: list[str],
) -> Optional[BlockMatch]:
    """Find the Terraform block managing azure_name using three deterministic passes.

    Args:
        tf_files:   Fetched .tf (and .tfvars) files from the IaC repo.
        azure_name: Azure resource name — the last segment of the ARM resource ID.
        tf_types:   TF resource types to scan for (from ARM_TO_TF_TYPE lookup).

    Returns:
        BlockMatch on success, None if not found.
    """
    logger.info(
        "tf_block_finder: searching %d file(s) for '%s' (types: %s)",
        len(tf_files), azure_name, tf_types,
    )

    # Pass 1 — exact literal match
    result = _scan_files_for_match(tf_files, tf_types, azure_name, "exact", _name_matches_exact)
    if result:
        return result

    # Pass 2 — static-prefix / interpolation match
    result = _scan_files_for_match(tf_files, tf_types, azure_name, "prefix", _name_matches_interpolated)
    if result:
        return result

    # Pass 3 — resolve tfvars, then exact match on resolved name
    vars_dict = _parse_tfvars(tf_files)
    if vars_dict:
        logger.info("tf_block_finder [tfvars]: resolved vars: %s", sorted(vars_dict))

        def resolved_match(name_value: str, _az: str) -> bool:
            resolved = _resolve_interpolation(name_value, vars_dict)
            return _name_matches_exact(resolved, azure_name)

        result = _scan_files_for_match(tf_files, tf_types, azure_name, "tfvars", resolved_match)
        if result:
            return result

    logger.info(
        "tf_block_finder: no deterministic match for '%s' across %d file(s)",
        azure_name, len(tf_files),
    )
    return None


def get_attribute_value(block: BlockMatch, attribute: str) -> Optional[str]:
    """Extract the current value of a top-level string attribute from a block.

    Only handles simple ``attribute = "value"`` patterns.
    Returns None for variable references (``attribute = var.x``), nested
    blocks, or attributes that are not present.
    """
    pattern = re.compile(
        r"^\s*" + re.escape(attribute) + r'\s*=\s*"([^"]*)"',
        re.MULTILINE,
    )
    m = pattern.search(block.raw_block)
    return m.group(1) if m else None


# =============================================================================
# Public async API (LLM-backed)
# =============================================================================

async def find_tf_block_with_llm(
    tf_files: list[TfFile],
    azure_name: str,
    tf_types: list[str],
) -> Optional[BlockMatch]:
    """LLM fallback: ask the model which Terraform block manages azure_name.

    Sends only the candidate blocks' logical names and name-field values —
    not the full file content — to minimise token cost.  Uses a single
    chat completion call (not an agent loop).

    Returns:
        BlockMatch if the LLM identifies a confident match, None otherwise.
    """
    from src.config import settings  # noqa: PLC0415

    if settings.use_local_mocks or not settings.azure_openai_endpoint:
        logger.debug("tf_block_finder [LLM]: skipping (mocks=%s, endpoint=%s)",
                     settings.use_local_mocks, bool(settings.azure_openai_endpoint))
        return None

    # Build candidate list
    candidates = []
    for tf_file in tf_files:
        for tf_type in tf_types:
            for block in _extract_blocks(tf_file.content, tf_type):
                candidates.append({
                    "tf_type": tf_type,
                    "logical_name": block["logical_name"],
                    "name_value": block["fields"].get("name", "(no name field)"),
                    "file_path": tf_file.file_path,
                    "start_line": block["start_line"],
                    "end_line": block["end_line"],
                    "lines": block["lines"],
                    "fields": block["fields"],
                })

    if not candidates:
        logger.info("tf_block_finder [LLM]: no candidate blocks of types %s", tf_types)
        return None

    candidate_list = "\n".join(
        f"  {i + 1}. {c['tf_type']}.{c['logical_name']}"
        f" (name = \"{c['name_value']}\") in {c['file_path']}"
        for i, c in enumerate(candidates)
    )
    prompt = (
        f"You are a Terraform expert. I have an Azure resource named '{azure_name}'. "
        f"I need to find which Terraform resource block manages it.\n\n"
        f"Candidates:\n{candidate_list}\n\n"
        f"Which candidate (by number) most likely manages '{azure_name}'? "
        f"Terraform names often use variable interpolation like ${{var.suffix}} "
        f"where the suffix value matches the end of the Azure resource name.\n\n"
        f"Reply with ONLY JSON: "
        f'{{\"candidate\": <1-based number or 0 if none match>, \"reason\": \"<brief>\"}}'
        f". Use 0 if you are not confident."
    )

    try:
        import json  # noqa: PLC0415
        from openai import AsyncAzureOpenAI  # noqa: PLC0415
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider  # noqa: PLC0415

        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
        )
        client = AsyncAzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version=settings.azure_openai_api_version,
            timeout=30.0,
        )
        resp = await client.chat.completions.create(
            model=settings.azure_openai_deployment,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
            temperature=0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content.strip())
        idx = int(data.get("candidate", 0)) - 1  # 1-indexed → 0-indexed

        if 0 <= idx < len(candidates):
            c = candidates[idx]
            logger.info(
                "tf_block_finder [LLM]: matched %s.%s — %s",
                c["tf_type"], c["logical_name"], data.get("reason", ""),
            )
            tf_file = next(f for f in tf_files if f.file_path == c["file_path"])
            return BlockMatch(
                tf_type=c["tf_type"],
                logical_name=c["logical_name"],
                file_path=c["file_path"],
                file_sha=tf_file.file_sha,
                start_line=c["start_line"],
                end_line=c["end_line"],
                raw_block="\n".join(c["lines"]),
                name_value=c["name_value"],
            )

        logger.info(
            "tf_block_finder [LLM]: no confident match (candidate=%s)", data.get("candidate")
        )
        return None

    except Exception as exc:  # noqa: BLE001
        logger.warning("tf_block_finder [LLM]: call failed — %s", exc)
        return None


async def resolve_config_change_with_llm(
    block: BlockMatch,
    action_reason: str,
    resource_name: str,
) -> Optional[dict]:
    """For UPDATE_CONFIG: ask the LLM what attribute to change and to what value.

    Sends the found TF block + agent reason to the model and asks for a
    single structured attribute:value recommendation.

    Args:
        block:         The already-located BlockMatch.
        action_reason: The agent's reason string.
        resource_name: Azure resource name (for context in the prompt).

    Returns:
        dict with keys: attribute, current_value, proposed_value, confidence
        or None if the LLM cannot determine a confident change.
    """
    from src.config import settings  # noqa: PLC0415

    if settings.use_local_mocks or not settings.azure_openai_endpoint:
        return None

    prompt = (
        f"You are a Terraform expert. An AI governance agent identified an issue with "
        f"the Azure resource '{resource_name}' and wants to update its configuration.\n\n"
        f"Agent's reason: \"{action_reason}\"\n\n"
        f"The Terraform block managing this resource:\n"
        f"```hcl\n{block.raw_block[:1500]}\n```\n\n"
        f"What single Terraform attribute should be changed, and to what value, "
        f"to address the issue? Only suggest attributes with a simple string or number "
        f"value (not nested blocks). Be specific and conservative.\n\n"
        f"Reply with ONLY JSON: "
        f'{{\"attribute\": \"<name>\", \"current_value\": \"<current>\", '
        f'\"proposed_value\": \"<new>\", \"confidence\": \"high|medium|low\"}}'
        f". Use confidence=low and leave proposed_value empty string if unsure."
    )

    try:
        import json  # noqa: PLC0415
        from openai import AsyncAzureOpenAI  # noqa: PLC0415
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider  # noqa: PLC0415

        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
        )
        client = AsyncAzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version=settings.azure_openai_api_version,
            timeout=30.0,
        )
        resp = await client.chat.completions.create(
            model=settings.azure_openai_deployment,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content.strip())

        if data.get("attribute") and data.get("proposed_value"):
            logger.info(
                "tf_block_finder [config_llm]: %s → %s = %r (confidence=%s)",
                resource_name, data["attribute"], data["proposed_value"],
                data.get("confidence"),
            )
            return data

        logger.info(
            "tf_block_finder [config_llm]: LLM returned low confidence or empty proposed_value"
        )
        return None

    except Exception as exc:  # noqa: BLE001
        logger.warning("tf_block_finder [config_llm]: call failed — %s", exc)
        return None
