"""Reading and writing the operator's local configuration file.

The UI edits the same JSON configuration file the CLI accepts through ``--config``,
so a setting changed in the browser applies to the next CLI invocation too.

Secrets never travel back to the browser. The API reports only *whether* a value is
configured and *where* it came from, plus a masked hint so the operator can tell one
key from another.
"""

from __future__ import annotations

import json
import os
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from typing import Any

from ..config import SECRET_KEYS, AdvisorConfig, Config, env_override_for
from ..errors import ConfigurationError
from ..util import to_pretty_json

#: Settings the UI may write. Anything outside this list is rejected rather than
#: silently ignored, so a typo in a request cannot look like a successful save.
EDITABLE_SETTINGS: dict[str, str] = {
    "storage_root": "path",
    "state_db": "path",
    "reports_dir": "path",
    "contact_email": "secret",
    "xml_policy": "choice:preferred,required,disabled",
    "log_level": "choice:DEBUG,INFO,WARNING,ERROR",
    "log_format": "choice:text,json",
    "openalex.api_key": "secret",
    "openalex.allow_keyless": "bool",
    "openalex.per_page": "int",
    "openalex.daily_credit_ceiling": "int_or_null",
    "openalex.requests_per_second": "float",
    "openalex.enabled": "bool",
    "europe_pmc.enabled": "bool",
    "europe_pmc.requests_per_second": "float",
    "unpaywall.enabled": "bool",
    "unpaywall.requests_per_second": "float",
    "advisor.enabled": "bool",
    "advisor.api_key": "secret",
    "advisor.model": "choice:claude-opus-5,claude-sonnet-5,claude-haiku-4-5",
    "advisor.effort": "choice:default,low,medium,high,xhigh,max",
    "downloads.concurrency": "int",
    "downloads.max_download_size_bytes": "int",
    "downloads.min_pdf_size_bytes": "int",
    "retry.max_attempts": "int",
    "retry.backoff_initial_seconds": "float",
    "retry.backoff_max_seconds": "float",
}

def mask_secret(value: str | None, *, kind: str) -> str | None:
    """Return a hint that identifies a secret without disclosing it."""
    if not value:
        return None
    if kind == "email":
        local, _, domain = value.partition("@")
        head = local[:1] if local else ""
        return f"{head}{'*' * max(3, len(local) - 1)}@{domain}" if domain else "***"
    tail = value[-4:] if len(value) > 8 else ""
    return f"{'*' * 8}{tail}"


def read_file(path: Path) -> dict[str, Any]:
    """Load the operator's config file, or an empty mapping when there is none."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"{path} must contain a JSON object")
    return data


def write_file(path: Path, data: dict[str, Any]) -> None:
    """Write the config file atomically, creating the directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(to_pretty_json(data), encoding="utf-8")
    os.replace(temporary, path)


def _get_dotted(data: dict[str, Any], dotted: str) -> Any:
    cursor: Any = data
    for key in dotted.split("."):
        if not isinstance(cursor, dict) or key not in cursor:
            return None
        cursor = cursor[key]
    return cursor


def _set_dotted(data: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    cursor = data
    for key in keys[:-1]:
        node = cursor.get(key)
        if not isinstance(node, dict):
            node = {}
            cursor[key] = node
        cursor = node
    if value is None:
        cursor.pop(keys[-1], None)
    else:
        cursor[keys[-1]] = value


def _config_value(config: Config, dotted: str) -> Any:
    cursor: Any = config
    for key in dotted.split("."):
        cursor = getattr(cursor, key, None)
        if cursor is None:
            return None
    return cursor


def coerce(dotted: str, spec: str, raw: Any) -> Any:
    """Validate and coerce one incoming setting value."""
    if spec.startswith("choice:"):
        allowed = spec.split(":", 1)[1].split(",")
        value = str(raw).strip()
        if value not in allowed:
            raise ConfigurationError(f"{dotted} must be one of {', '.join(allowed)}")
        return value
    if spec == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ConfigurationError(f"{dotted} must be true or false")
    if spec in ("int", "int_or_null"):
        if raw in (None, "", "null"):
            if spec == "int_or_null":
                return None
            raise ConfigurationError(f"{dotted} may not be empty")
        try:
            return int(str(raw).strip())
        except ValueError as exc:
            raise ConfigurationError(f"{dotted} must be a whole number") from exc
    if spec == "float":
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"{dotted} must be a number") from exc
    if spec == "path":
        value = str(raw).strip()
        if not value:
            raise ConfigurationError(f"{dotted} may not be empty")
        return value
    if spec == "secret":
        value = str(raw).strip()
        return value or None
    return str(raw)


def apply_updates(
    path: Path, updates: dict[str, Any], *, env: dict[str, str] | None = None
) -> dict[str, Any]:
    """Merge *updates* into the config file after validating every key and value.

    The merged configuration is loaded through :meth:`Config.load` and validated
    before the file is written, so the UI cannot persist a configuration that the
    CLI would then refuse to start with.

    The probe honours the same environment the running process does (*env* defaults
    to ``os.environ``). A secret supplied only through ``HARVESTER_CONTACT_EMAIL`` or
    ``HARVESTER_OPENALEX_API_KEY`` therefore counts as configured here, exactly as it
    does at run time, and stays out of the file.
    """
    unknown = sorted(set(updates) - set(EDITABLE_SETTINGS))
    if unknown:
        raise ConfigurationError(f"not an editable setting: {', '.join(unknown)}")

    # A setting the environment controls is refused rather than written. The file
    # value would have no effect while the variable stands, so accepting it would
    # record a value the loader ignores — and a stale browser tab submitting the
    # displayed (environment) value would quietly persist it as if it were chosen.
    locked = sorted(
        f"{dotted} ({name})"
        for dotted in updates
        if (name := env_override_for(dotted, env)) is not None
    )
    if locked:
        raise ConfigurationError(
            "set by an environment variable and not editable here: " + ", ".join(locked)
        )

    data = read_file(path)
    for dotted, raw in updates.items():
        spec = EDITABLE_SETTINGS[dotted]
        value = coerce(dotted, spec, raw)
        if spec == "secret" and value is None:
            _set_dotted(data, dotted, None)  # explicit clear
        else:
            _set_dotted(data, dotted, value)

    # Prove the result is loadable and internally consistent before writing it.
    probe_path = path.with_name(path.name + ".probe")
    try:
        probe_path.write_text(to_pretty_json(data), encoding="utf-8")
        candidate = Config.load(config_path=probe_path, env=env)
        candidate.validate(require_openalex=False)
    finally:
        probe_path.unlink(missing_ok=True)

    write_file(path, data)
    return data


def describe(
    config: Config, path: Path, *, env: dict[str, str] | None = None
) -> dict[str, Any]:
    """Operator-facing settings snapshot. Contains no secret values.

    Which settings the environment controls is answered by :func:`env_override_for`,
    the same derivation :meth:`Config.load` uses. It is deliberately not a list kept
    here: a local copy covering only the secrets is how this drifted before, reporting
    an environment-controlled value as an ordinary editable file value — after which
    saving the form wrote the environment's value into the file.
    """
    file_data = read_file(path)
    settings: list[dict[str, Any]] = []

    for dotted, spec in EDITABLE_SETTINGS.items():
        in_file = _get_dotted(file_data, dotted) is not None
        env_name = env_override_for(dotted, env)
        from_env = env_name is not None
        effective = _config_value(config, dotted)

        entry: dict[str, Any] = {
            "key": dotted,
            "spec": spec,
            "source": "environment" if from_env else ("file" if in_file else "default"),
            "editable": not from_env,
        }
        if env_name:
            # The variable name, never its value: it tells the operator where to go
            # and change it, which "set by environment variable" alone does not.
            entry["env_var"] = env_name
        if spec == "secret":
            entry["configured"] = bool(effective)
            entry["masked"] = mask_secret(
                str(effective) if effective else None,
                kind="email" if "email" in dotted else "key",
            )
            entry["value"] = None
        else:
            entry["value"] = str(effective) if isinstance(effective, Path) else effective
        settings.append(entry)

    return {
        "config_path": str(path),
        "config_file_exists": path.exists(),
        "settings": settings,
        "effective": config.redacted_dict(),
    }


def provider_readiness(config: Config) -> list[dict[str, Any]]:
    """Whether each provider is ready, with a plain-language explanation.

    The judgement itself comes from :meth:`Config.validate`; this only presents it.
    """
    providers: list[dict[str, Any]] = []

    if not config.openalex.enabled:
        openalex = {"state": "disabled", "detail": "OpenAlex is switched off in Settings."}
    elif config.openalex.api_key:
        openalex = {"state": "ready", "detail": "API key configured."}
    elif config.openalex.allow_keyless:
        openalex = {
            "state": "warning",
            "detail": (
                "Running without an API key. OpenAlex allows only a small daily testing "
                "allowance this way — fine for trying things out, not for real harvesting."
            ),
            "action": "Add an OpenAlex API key in Settings.",
        }
    else:
        openalex = {
            "state": "blocked",
            "detail": (
                "OpenAlex requires an API key for normal use. Harvesting cannot start "
                "until one is configured."
            ),
            "action": "Add an OpenAlex API key in Settings.",
        }
    openalex.update(id="openalex", name="OpenAlex", role="Primary discovery", required=True)
    providers.append(openalex)

    europe_pmc = (
        {"state": "ready", "detail": "No credentials required."}
        if config.europe_pmc.enabled
        else {"state": "disabled", "detail": "Cross-checking and XML full text are switched off."}
    )
    europe_pmc.update(
        id="europe_pmc",
        name="Europe PMC",
        role="Cross-check and XML full text",
        required=False,
    )
    providers.append(europe_pmc)

    if not config.unpaywall.enabled:
        unpaywall = {"state": "disabled", "detail": "The Unpaywall fallback is switched off."}
    elif config.contact_email:
        unpaywall = {"state": "ready", "detail": "Contact email configured."}
    else:
        unpaywall = {
            "state": "blocked",
            "detail": (
                "Unpaywall's terms require a contact email address. Without one the "
                "fallback cannot be used."
            ),
            "action": "Add a contact email in Settings, or switch Unpaywall off.",
        }
    unpaywall.update(
        id="unpaywall", name="Unpaywall", role="Fallback OA locations", required=False
    )
    providers.append(unpaywall)
    return providers


def can_start_harvest(config: Config) -> tuple[bool, list[str]]:
    """Whether a harvest may start, and the reasons if not.

    The query advisor is deliberately excluded from the judgement. It takes no part in
    discovery or acquisition, so no advisor setting — missing, disabled or invalid —
    may ever stop a harvest (Assisted Search V1 sections 8 and 52). Advisor problems
    are reported separately by :func:`harvester.advisor.advisor_readiness`.
    """
    problems: list[str] = []
    try:
        replace(config, advisor=AdvisorConfig()).validate(require_openalex=True)
    except ConfigurationError as exc:
        problems.append(str(exc))
    return not problems, problems


def _dataclass_keys(instance: Any) -> list[str]:  # pragma: no cover - introspection aid
    return [f.name for f in fields(instance)] if is_dataclass(instance) else []


__all__ = [
    "EDITABLE_SETTINGS",
    "SECRET_KEYS",
    "apply_updates",
    "can_start_harvest",
    "describe",
    "mask_secret",
    "provider_readiness",
    "read_file",
    "write_file",
]
