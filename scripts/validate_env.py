#!/usr/bin/env python3
"""
validate_env.py — check that .env is in sync with src/config.py.

Compares the keys defined in src/config.py (Settings class) against
the keys present in .env.example and optionally a local .env file.

Run from the project root:
    python scripts/validate_env.py
    python scripts/validate_env.py --env .env  # also check real .env

Exit code 0 = all clear.  Non-zero = drift found.
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Parse config.py field names → expected env var names
# ---------------------------------------------------------------------------

def get_config_vars() -> dict[str, str]:
    """Return {ENV_VAR_NAME: default_value} from src/config.py."""
    config_text = (ROOT / "src" / "config.py").read_text(encoding="utf-8")
    # Match lines like:    some_field: type = "default"
    # or                   some_field: type = True  / 0  / etc.
    pattern = re.compile(
        r"^\s{4}(\w+)\s*:\s*[\w\[\]|\"' ]+\s*=\s*(.+?)(?:\s*#.*)?$",
        re.MULTILINE,
    )
    result = {}
    for m in pattern.finditer(config_text):
        field = m.group(1)
        default = m.group(2).strip().strip('"').strip("'")
        # Skip private / dunder / model_config
        if field.startswith("_") or field == "model_config":
            continue
        env_var = field.upper()
        result[env_var] = default
    return result


# ---------------------------------------------------------------------------
# Parse an env file → set of defined keys
# ---------------------------------------------------------------------------

def get_env_keys(path: Path) -> set[str]:
    """Return the set of variable names defined in an env file."""
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            keys.add(key)
    return keys


# ---------------------------------------------------------------------------
# Security checks
# ---------------------------------------------------------------------------

SENSITIVE_PATTERNS = [
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]+"),
    re.compile(r"AAAA[A-Za-z0-9+/]{20,}"),  # base64-looking tokens
]

SENSITIVE_VARS = {"API_KEY", "ALERT_WEBHOOK_SECRET", "GITHUB_TOKEN",
                  "AZURE_OPENAI_API_KEY", "AZURE_SEARCH_API_KEY",
                  "COSMOS_KEY", "SLACK_WEBHOOK_URL"}


def check_example_for_real_secrets(path: Path) -> list[str]:
    """Warn if .env.example contains actual secrets (should only have placeholders)."""
    issues = []
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not value:
            continue
        for pattern in SENSITIVE_PATTERNS:
            if pattern.search(value):
                issues.append(
                    f"  CRITICAL: {path.name} line contains a real secret in {key!r} — "
                    f"replace with a placeholder"
                )
    return issues


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Validate .env against config.py")
    parser.add_argument("--env", metavar="FILE", help="Also validate this .env file")
    args = parser.parse_args()

    ok = True
    example_path = ROOT / ".env.example"

    # --- 1. Load config.py fields ---
    config_vars = get_config_vars()
    print(f"config.py defines {len(config_vars)} settings fields\n")

    # --- 2. Check .env.example coverage ---
    example_keys = get_env_keys(example_path)

    in_config_not_example = set(config_vars) - example_keys
    in_example_not_config = example_keys - set(config_vars)

    if in_config_not_example:
        print("HIGH — Variables in config.py missing from .env.example:")
        for k in sorted(in_config_not_example):
            print(f"  {k}  (default: {config_vars[k]!r})")
        ok = False
    else:
        print("OK — .env.example covers all config.py fields")

    if in_example_not_config:
        print("\nMEDIUM — Variables in .env.example not found in config.py (stale?):")
        for k in sorted(in_example_not_config):
            print(f"  {k}")
        ok = False

    # --- 3. Check .env.example has no real secrets ---
    secret_issues = check_example_for_real_secrets(example_path)
    if secret_issues:
        print("\nCRITICAL — Real secrets found in .env.example:")
        for issue in secret_issues:
            print(issue)
        ok = False
    else:
        print("\nOK — .env.example contains no real secrets")

    # --- 4. If a real .env was provided, check it too ---
    if args.env:
        env_path = Path(args.env)
        if not env_path.exists():
            print(f"\nWARN — {env_path} does not exist; skipping real-env checks")
        else:
            env_keys = get_env_keys(env_path)
            missing_security = SENSITIVE_VARS & set(config_vars) - env_keys
            if missing_security:
                print(f"\nMEDIUM — Security vars not set in {env_path.name} "
                      f"(will fall back to empty defaults — API will be open):")
                for k in sorted(missing_security):
                    print(f"  {k}")

            # Check if any value in .env looks like a placeholder
            env_text = env_path.read_text(encoding="utf-8")
            for line in env_text.splitlines():
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if "your-" in value or "your_" in value:
                    print(f"\nWARN — {key} in {env_path.name} looks like an unfilled placeholder: {value!r}")
                    ok = False

            # Check for IAC_TERRAFORM_PATH pointing to non-existent path
            if "IAC_TERRAFORM_PATH" in env_keys:
                for line in env_text.splitlines():
                    if line.startswith("IAC_TERRAFORM_PATH="):
                        iac_path = line.split("=", 1)[1].strip()
                        if "terraform-prod" in iac_path:
                            print(
                                f"\nHIGH — IAC_TERRAFORM_PATH={iac_path!r} contains 'terraform-prod' "
                                f"which does not exist in ruriskry-iac-test. "
                                f"Should be 'infrastructure/terraform-demo'."
                            )
                            ok = False

    print()
    if ok:
        print("All checks passed.")
        return 0
    else:
        print("Issues found — see above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
