# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""Externalised configuration (MASTER_SPEC section 32).

Precedence, highest first:

    1. command-line arguments
    2. environment variables (``HARVESTER_*``)
    3. configuration file (JSON, ``--config`` or ``HARVESTER_CONFIG``)
    4. built-in defaults

Secrets (``openalex.api_key``, ``contact_email``) are supplied through environment
variables or an operator-owned config file that is excluded from source control. They
are never written to logs, run reports, sidecars or provenance.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .errors import ConfigurationError

DEFAULT_USER_AGENT_PRODUCT = "OpenAccessAPIHarvester/1.0"

#: Keys whose values must never be serialised into any operator-visible output.
SECRET_KEYS = frozenset({"api_key", "contact_email"})

#: Reasoning-effort levels an advisor model may be asked for.
ADVISOR_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})

#: Value meaning "ask for no particular effort": the parameter is left out of the
#: request entirely and the provider applies whatever default it has. Reasoning
#: effort is not a universal parameter — a model that does not take one rejects the
#: whole request rather than ignoring the field — so this is the setting to use with
#: any model that does not offer it. Which models those are is the provider's
#: business, not ours: the choice stays with the operator so that a new model never
#: needs a corresponding change here.
ADVISOR_EFFORT_UNSET = "default"

#: Everything ``advisor.effort`` may be set to.
ADVISOR_EFFORT_CHOICES = ADVISOR_EFFORT_LEVELS | {ADVISOR_EFFORT_UNSET}


@dataclass(slots=True)
class RetryConfig:
    """Retry/backoff policy (MASTER_SPEC section 26)."""

    max_attempts: int = 4
    backoff_initial_seconds: float = 2.0
    backoff_multiplier: float = 2.0
    backoff_max_seconds: float = 60.0
    jitter_ratio: float = 0.25
    #: Cap on ``Retry-After`` values honoured before treating the wait as unreasonable.
    max_retry_after_seconds: float = 300.0


@dataclass(slots=True)
class ProviderConfig:
    """Per-provider rate/concurrency settings (MASTER_SPEC section 27)."""

    enabled: bool = True
    concurrency: int = 2
    #: Sustained request rate ceiling. Defaults stay well below documented provider limits.
    requests_per_second: float = 2.0
    timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 10.0


@dataclass(slots=True)
class OpenAlexConfig(ProviderConfig):
    base_url: str = "https://api.openalex.org"
    api_key: str | None = None
    #: Deliberate opt-in for the tiny keyless testing allowance. Never for production.
    allow_keyless: bool = False
    per_page: int = 200
    #: Optional local safety ceiling on daily credits (SPEC_PATCH section 2).
    daily_credit_ceiling: int | None = None


@dataclass(slots=True)
class EuropePmcConfig(ProviderConfig):
    base_url: str = "https://www.ebi.ac.uk/europepmc/webservices/rest"
    #: Documented limit is 10 req/s and 500/min per IP; stay conservatively below it.
    requests_per_second: float = 3.0
    page_size: int = 25


@dataclass(slots=True)
class UnpaywallConfig(ProviderConfig):
    base_url: str = "https://api.unpaywall.org/v2"
    concurrency: int = 1
    requests_per_second: float = 1.0


@dataclass(slots=True)
class AdvisorConfig(ProviderConfig):
    """Query Advisor (Assisted Search V1).

    A bounded text-in/text-out model integration used only to turn a research
    question into a candidate discovery query. It is never consulted by the harvest
    pipeline itself: Conventional Search and every acquisition path work unchanged
    when it is disabled, unconfigured or unreachable.
    """

    base_url: str = "https://api.anthropic.com"
    api_key: str | None = None
    #: Anthropic Messages API model id. Fixed ids, never date-suffixed.
    model: str = "claude-opus-5"
    #: API version header value required by the Messages API.
    api_version: str = "2023-06-01"
    #: Ceiling on the advisor response. Covers reasoning plus the JSON answer.
    max_tokens: int = 2000
    #: Reasoning effort. The task is small, so the cheapest useful level is enough.
    #: ``ADVISOR_EFFORT_UNSET`` leaves the parameter out of the request, which is what
    #: a model that does not accept one needs.
    effort: str = "low"
    #: Recorded in run provenance so an old run stays explainable.
    prompt_version: str = "assisted-search-v1"
    concurrency: int = 1
    requests_per_second: float = 1.0
    timeout_seconds: float = 90.0


@dataclass(slots=True)
class DownloadConfig:
    concurrency: int = 4
    requests_per_second: float = 4.0
    timeout_seconds: float = 120.0
    connect_timeout_seconds: float = 15.0
    max_download_size_bytes: int = 200 * 1024 * 1024
    min_pdf_size_bytes: int = 1024
    min_xml_size_bytes: int = 256
    max_redirects: int = 5
    #: Allow loopback/private acquisition targets. Enabled only by the test harness.
    allow_private_hosts: bool = False


@dataclass(slots=True)
class Config:
    """The complete effective configuration for one invocation."""

    storage_root: Path = Path("corpus")
    state_db: Path = Path("state/harvester.sqlite3")
    reports_dir: Path = Path("reports")
    contact_email: str | None = None
    user_agent: str | None = None
    log_level: str = "INFO"
    log_format: str = "text"  # "text" | "json"
    log_file: Path | None = None

    xml_policy: str = "preferred"  # "preferred" | "required" | "disabled"

    openalex: OpenAlexConfig = field(default_factory=OpenAlexConfig)
    europe_pmc: EuropePmcConfig = field(default_factory=EuropePmcConfig)
    unpaywall: UnpaywallConfig = field(default_factory=UnpaywallConfig)
    advisor: AdvisorConfig = field(default_factory=AdvisorConfig)
    downloads: DownloadConfig = field(default_factory=DownloadConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)

    # ---------------------------------------------------------------- construction

    @classmethod
    def load(
        cls,
        *,
        config_path: Path | None = None,
        env: dict[str, str] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> Config:
        """Build the effective configuration honouring the documented precedence."""
        environment = os.environ if env is None else env
        config = cls()

        path = config_path
        if path is None and environment.get("HARVESTER_CONFIG"):
            path = Path(environment["HARVESTER_CONFIG"])
        if path is not None:
            config = _apply_mapping(config, _read_config_file(path))

        config = _apply_mapping(config, _mapping_from_env(environment))

        if overrides:
            config = _apply_mapping(config, _expand_dotted(overrides))

        config.storage_root = Path(config.storage_root)
        config.state_db = Path(config.state_db)
        config.reports_dir = Path(config.reports_dir)
        if config.log_file is not None:
            config.log_file = Path(config.log_file)
        return config

    # ------------------------------------------------------------------ validation

    def validate(self, *, require_openalex: bool = True) -> None:
        """Raise :class:`ConfigurationError` when the configuration cannot be used."""
        if self.xml_policy not in ("preferred", "required", "disabled"):
            raise ConfigurationError(
                f"xml_policy must be preferred|required|disabled, got {self.xml_policy!r}"
            )
        if self.log_format not in ("text", "json"):
            raise ConfigurationError(f"log_format must be text|json, got {self.log_format!r}")

        for name, provider in (
            ("openalex", self.openalex),
            ("europe_pmc", self.europe_pmc),
            ("unpaywall", self.unpaywall),
            ("advisor", self.advisor),
        ):
            if provider.concurrency < 1:
                raise ConfigurationError(f"{name}.concurrency must be >= 1")
            if provider.requests_per_second <= 0:
                raise ConfigurationError(f"{name}.requests_per_second must be > 0")
            if provider.timeout_seconds <= 0:
                raise ConfigurationError(f"{name}.timeout_seconds must be > 0")

        if self.downloads.concurrency < 1:
            raise ConfigurationError("downloads.concurrency must be >= 1")
        if self.downloads.max_download_size_bytes < 1:
            raise ConfigurationError("downloads.max_download_size_bytes must be >= 1")
        if self.downloads.min_pdf_size_bytes < 1:
            raise ConfigurationError("downloads.min_pdf_size_bytes must be >= 1")
        if self.retry.max_attempts < 1:
            raise ConfigurationError("retry.max_attempts must be >= 1")
        if self.retry.backoff_initial_seconds < 0:
            raise ConfigurationError("retry.backoff_initial_seconds must be >= 0")
        if not 0 <= self.retry.jitter_ratio <= 1:
            raise ConfigurationError("retry.jitter_ratio must be within [0, 1]")
        # An empty value is spelled ADVISOR_EFFORT_UNSET, but a hand-edited config
        # file that leaves the field blank means the same thing and is accepted.
        if self.advisor.effort and self.advisor.effort not in ADVISOR_EFFORT_CHOICES:
            raise ConfigurationError(
                "advisor.effort must be one of " + "|".join(sorted(ADVISOR_EFFORT_CHOICES))
            )
        if self.advisor.max_tokens < 1:
            raise ConfigurationError("advisor.max_tokens must be >= 1")
        if not 1 <= self.openalex.per_page <= 200:
            raise ConfigurationError("openalex.per_page must be within 1..200")
        if (
            self.openalex.daily_credit_ceiling is not None
            and self.openalex.daily_credit_ceiling < 1
        ):
            raise ConfigurationError("openalex.daily_credit_ceiling must be >= 1 when set")

        if require_openalex and self.openalex.enabled:
            if not self.openalex.api_key and not self.openalex.allow_keyless:
                raise ConfigurationError(
                    "OpenAlex requires an API key for production use (verified 2026-02-13 "
                    "policy change). Set HARVESTER_OPENALEX_API_KEY, or pass "
                    "--allow-keyless-openalex to use the tiny keyless testing allowance."
                )
        if self.unpaywall.enabled and not self.contact_email:
            raise ConfigurationError(
                "Unpaywall requires a contact email (HARVESTER_CONTACT_EMAIL or "
                "--contact-email). Disable it with --no-unpaywall if unavailable."
            )

    # ------------------------------------------------------------------- accessors

    def effective_user_agent(self) -> str:
        """Identifiable User-Agent (MASTER_SPEC section 28).

        The contact email is a courtesy identification here, not an access-control
        token. It is omitted when not configured rather than invented.
        """
        if self.user_agent:
            return self.user_agent
        if self.contact_email:
            return f"{DEFAULT_USER_AGENT_PRODUCT} (mailto:{self.contact_email})"
        return DEFAULT_USER_AGENT_PRODUCT

    def redacted_dict(self) -> dict[str, Any]:
        """Configuration snapshot safe for logs, reports and state (MASTER_SPEC 37)."""
        return _redact(asdict(self))


# --------------------------------------------------------------------------- helpers


def _read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigurationError(f"configuration file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"invalid JSON in configuration file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"configuration file {path} must contain a JSON object")
    return raw


#: environment variable -> dotted configuration path
_ENV_MAP: dict[str, str] = {
    "HARVESTER_STORAGE_ROOT": "storage_root",
    "HARVESTER_STATE_DB": "state_db",
    "HARVESTER_REPORTS_DIR": "reports_dir",
    "HARVESTER_CONTACT_EMAIL": "contact_email",
    "HARVESTER_USER_AGENT": "user_agent",
    "HARVESTER_LOG_LEVEL": "log_level",
    "HARVESTER_LOG_FORMAT": "log_format",
    "HARVESTER_LOG_FILE": "log_file",
    "HARVESTER_XML_POLICY": "xml_policy",
    "HARVESTER_OPENALEX_API_KEY": "openalex.api_key",
    "HARVESTER_OPENALEX_BASE_URL": "openalex.base_url",
    "HARVESTER_OPENALEX_ENABLED": "openalex.enabled",
    "HARVESTER_OPENALEX_CONCURRENCY": "openalex.concurrency",
    "HARVESTER_OPENALEX_RPS": "openalex.requests_per_second",
    "HARVESTER_OPENALEX_PER_PAGE": "openalex.per_page",
    "HARVESTER_OPENALEX_ALLOW_KEYLESS": "openalex.allow_keyless",
    "HARVESTER_OPENALEX_DAILY_CREDIT_CEILING": "openalex.daily_credit_ceiling",
    "HARVESTER_EUROPE_PMC_BASE_URL": "europe_pmc.base_url",
    "HARVESTER_EUROPE_PMC_ENABLED": "europe_pmc.enabled",
    "HARVESTER_EUROPE_PMC_CONCURRENCY": "europe_pmc.concurrency",
    "HARVESTER_EUROPE_PMC_RPS": "europe_pmc.requests_per_second",
    "HARVESTER_UNPAYWALL_BASE_URL": "unpaywall.base_url",
    "HARVESTER_UNPAYWALL_ENABLED": "unpaywall.enabled",
    "HARVESTER_UNPAYWALL_CONCURRENCY": "unpaywall.concurrency",
    "HARVESTER_UNPAYWALL_RPS": "unpaywall.requests_per_second",
    # The advisor vendor's own variable is honoured for convenience; the
    # HARVESTER_-prefixed name is listed after it so an explicit override wins.
    "ANTHROPIC_API_KEY": "advisor.api_key",
    "HARVESTER_ADVISOR_API_KEY": "advisor.api_key",
    "HARVESTER_ADVISOR_ENABLED": "advisor.enabled",
    "HARVESTER_ADVISOR_BASE_URL": "advisor.base_url",
    "HARVESTER_ADVISOR_MODEL": "advisor.model",
    "HARVESTER_ADVISOR_MAX_TOKENS": "advisor.max_tokens",
    "HARVESTER_ADVISOR_EFFORT": "advisor.effort",
    "HARVESTER_ADVISOR_TIMEOUT": "advisor.timeout_seconds",
    "HARVESTER_DOWNLOAD_CONCURRENCY": "downloads.concurrency",
    "HARVESTER_DOWNLOAD_TIMEOUT": "downloads.timeout_seconds",
    "HARVESTER_MAX_DOWNLOAD_SIZE_BYTES": "downloads.max_download_size_bytes",
    "HARVESTER_MIN_PDF_SIZE_BYTES": "downloads.min_pdf_size_bytes",
    "HARVESTER_ALLOW_PRIVATE_HOSTS": "downloads.allow_private_hosts",
    "HARVESTER_RETRY_MAX_ATTEMPTS": "retry.max_attempts",
    "HARVESTER_RETRY_BACKOFF_INITIAL": "retry.backoff_initial_seconds",
    "HARVESTER_RETRY_BACKOFF_MAX": "retry.backoff_max_seconds",
    "HARVESTER_RETRY_JITTER_RATIO": "retry.jitter_ratio",
}


def env_variable_names() -> tuple[str, ...]:
    """Every environment variable this loader reads, plus the one naming the file.

    Published so a caller can neutralise the whole set in one place — a test process
    that inherits the operator's real credentials would otherwise validate the host
    machine rather than the code. Derived from the same table the loader uses, so it
    cannot fall behind when a new variable is added.
    """
    return (*_ENV_MAP, "HARVESTER_CONFIG")


def env_names_for(dotted: str) -> list[str]:
    """Every environment variable name the loader maps onto *dotted*.

    In the order :data:`_ENV_MAP` declares them, which is the order they are applied
    in, so the last one wins.
    """
    return [name for name, path in _ENV_MAP.items() if path == dotted]


def env_override_for(dotted: str, environment: dict[str, str] | None = None) -> str | None:
    """The environment variable currently overriding *dotted*, or ``None``.

    Derived from the same table and the same present-and-non-empty rule that
    :func:`_mapping_from_env` applies, so a caller asking "is this setting under
    environment control?" can never disagree with what :meth:`Config.load` actually
    did. That question has one answer, and this is where it is computed.
    """
    env = os.environ if environment is None else environment
    winner: str | None = None
    for name in env_names_for(dotted):
        if env.get(name):
            winner = name
    return winner


def _mapping_from_env(environment: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for env_key, dotted in _ENV_MAP.items():
        if env_key in environment and environment[env_key] != "":
            _set_dotted(result, dotted, environment[env_key])
    return result


def _expand_dotted(overrides: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for dotted, value in overrides.items():
        if value is None:
            continue
        _set_dotted(result, dotted, value)
    return result


def _set_dotted(target: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    cursor = target
    for key in keys[:-1]:
        cursor = cursor.setdefault(key, {})
    cursor[keys[-1]] = value


def _apply_mapping(config: Config, mapping: dict[str, Any]) -> Config:
    """Return a copy of *config* with *mapping* applied, coercing to declared types."""
    if not mapping:
        return config
    updates: dict[str, Any] = {}
    field_types = {f.name: f.type for f in fields(config)}
    for key, value in mapping.items():
        if key not in field_types:
            raise ConfigurationError(f"unknown configuration key: {key!r}")
        current = getattr(config, key)
        if isinstance(current, (ProviderConfig, DownloadConfig, RetryConfig)):
            if not isinstance(value, dict):
                raise ConfigurationError(f"configuration section {key!r} must be an object")
            updates[key] = _apply_section(current, key, value)
        else:
            updates[key] = _coerce_scalar(key, field_types[key], value)
    return replace(config, **updates)


def _apply_section(section: Any, section_name: str, mapping: dict[str, Any]) -> Any:
    field_types = {f.name: f.type for f in fields(section)}
    updates: dict[str, Any] = {}
    for key, value in mapping.items():
        if key not in field_types:
            raise ConfigurationError(f"unknown configuration key: {section_name}.{key}")
        updates[key] = _coerce_scalar(f"{section_name}.{key}", field_types[key], value)
    return replace(section, **updates)


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _coerce_scalar(name: str, declared: Any, value: Any) -> Any:
    """Coerce *value* to the type declared on the dataclass field."""
    declared_str = declared if isinstance(declared, str) else getattr(declared, "__name__", "")
    optional = "None" in declared_str

    if value is None:
        if optional:
            return None
        raise ConfigurationError(f"{name} may not be null")

    if "bool" in declared_str:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ConfigurationError(f"{name} must be a boolean, got {value!r}")

    if "Path" in declared_str:
        return Path(str(value))

    if "int" in declared_str and "float" not in declared_str:
        try:
            return int(str(value).strip())
        except ValueError as exc:
            raise ConfigurationError(f"{name} must be an integer, got {value!r}") from exc

    if "float" in declared_str:
        try:
            return float(str(value).strip())
        except ValueError as exc:
            raise ConfigurationError(f"{name} must be a number, got {value!r}") from exc

    return str(value)


def _redact(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: _redact(v, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if key in SECRET_KEYS and value:
        return "***REDACTED***"
    if isinstance(value, Path):
        return str(value)
    return value
