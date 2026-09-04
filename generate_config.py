"""Generate LiteLLM model pools from numbered API keys in a .env file.

Usage:
    python generate_config.py
    python generate_config.py --env .env --config config.yaml
    python generate_config.py --dry-run

The existing config supplies the model templates. For every template using a
key such as ``os.environ/GROQ_KEY_1``, this script finds all ``GROQ_KEY_N``
entries in .env and creates a matching model entry for each key. It therefore
preserves configured model IDs, aliases, and fallbacks while removing manual
copy/paste of key blocks.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

KEY_REFERENCE_RE = re.compile(r"^os\.environ/([A-Z][A-Z0-9_]*_\d+)$")
KEY_FAMILY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)_(\d+)$")


def _key_sort_key(name: str) -> tuple[str, int]:
    match = KEY_FAMILY_RE.match(name)
    return (match.group(1), int(match.group(2))) if match else (name, 0)


def discover_key_groups(env_path: str | os.PathLike[str]) -> dict[str, list[str]]:
    """Return non-empty numbered .env variables grouped by provider family."""
    values = dotenv_values(env_path)
    groups: dict[str, list[str]] = defaultdict(list)
    for name, value in values.items():
        match = KEY_FAMILY_RE.match(name or "")
        if match and value:
            groups[match.group(1)].append(name)
    return {family: sorted(names, key=_key_sort_key) for family, names in groups.items()}


def _template_family(entry: dict[str, Any]) -> str | None:
    params = entry.get("litellm_params")
    if not isinstance(params, dict):
        return None
    match = KEY_REFERENCE_RE.match(str(params.get("api_key", "")))
    return match.group(1).rsplit("_", 1)[0] if match else None


def expand_model_list(
    model_list: list[dict[str, Any]], key_groups: dict[str, list[str]]
) -> tuple[list[dict[str, Any]], set[str]]:
    """Clone each model/key-family template for every discovered numbered key.

    Returns the expanded model list, plus the set of key families that were
    discovered in .env but have no matching model template in config.yaml
    (so those keys could not produce any model entry).
    """
    expanded: list[dict[str, Any]] = []
    processed_families: set[str] = set()
    template_families: set[str] = set()
    for entry in model_list:
        if not isinstance(entry, dict):
            expanded.append(entry)
            continue
        family = _template_family(entry)
        if family is None:
            expanded.append(entry)
            continue
        template_families.add(family)
        if family in processed_families:
            continue

        templates = [candidate for candidate in model_list if _template_family(candidate) == family]
        keys = key_groups.get(family)
        if not keys:
            expanded.extend(templates)
        else:
            for key in keys:
                for template in templates:
                    expanded.append({
                        **template,
                        "litellm_params": {**template["litellm_params"], "api_key": f"os.environ/{key}"},
                    })
        processed_families.add(family)

    untemplated_families = set(key_groups) - template_families
    return expanded, untemplated_families

def generate_config(env_path: str | os.PathLike[str], config_path: str | os.PathLike[str]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Load, expand, and persist a LiteLLM config. Return config and key map.

    If .env contains numbered keys for a provider family with no matching
    model template in config.yaml, those keys cannot produce any model
    entry. Rather than silently dropping them, a warning is printed to
    stderr naming each such family so the gap is visible instead of hidden.
    """
    env_path, config_path = Path(env_path), Path(config_path)
    if not env_path.is_file():
        raise FileNotFoundError(f"Environment file not found: {env_path}")
    if not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with config_path.open(encoding="utf-8") as source:
        config = yaml.safe_load(source) or {}
    model_list = config.get("model_list")
    if not isinstance(model_list, list):
        raise ValueError("config.yaml must contain a model_list array")

    groups = discover_key_groups(env_path)
    expanded, untemplated_families = expand_model_list(model_list, groups)
    config["model_list"] = expanded
    if untemplated_families:
        for family in sorted(untemplated_families):
            print(
                f"Warning: {family}_N key(s) found in {env_path} but no model template "
                f"for '{family}' exists in {config_path}; these keys were not added to "
                "the generated config.",
                file=sys.stderr,
            )
    with config_path.open("w", encoding="utf-8", newline="\n") as target:
        yaml.safe_dump(config, target, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return config, groups

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Expand LiteLLM model pools from numbered .env API keys.")
    parser.add_argument("--env", default=".env", help="Path to the .env file (default: .env)")
    parser.add_argument("--config", default="config.yaml", help="Path to the LiteLLM config (default: config.yaml)")
    parser.add_argument("--dry-run", action="store_true", help="Show discovered key counts without writing config.yaml")
    args = parser.parse_args(argv)
    try:
        groups = discover_key_groups(args.env)
        if args.dry_run:
            summary = ", ".join(f"{family}: {len(keys)}" for family, keys in sorted(groups.items())) or "no usable numbered keys"
            print(f"Discovered key groups — {summary}")
            return 0
        config, _ = generate_config(args.env, args.config)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"Config generation failed: {error}", file=sys.stderr)
        return 1
    print(f"Updated {args.config}: {len(config['model_list'])} model entries generated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
