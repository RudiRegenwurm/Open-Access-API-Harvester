"""Unit tests: configuration precedence, validation and secret handling (AC-017)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harvester.config import Config, env_variable_names
from harvester.errors import ConfigurationError


def write_config(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "harvester.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ----------------------------------------------------- host environment isolation


def test_no_harvester_variable_is_visible_to_a_test():
    """These tests describe the code, not the machine they happen to run on.

    ``Config.load`` reads ``os.environ`` when it is given no explicit mapping, so a
    developer who exports a real ``HARVESTER_OPENALEX_API_KEY`` or
    ``HARVESTER_CONTACT_EMAIL`` used to change what the suite was testing: cases
    asserting that an unconfigured harvester refuses to start passed on a bare CI box
    and failed on the machine of the person who actually uses the tool. The
    ``isolated_environment`` fixture clears the whole set for the duration of each
    test; this is the assertion that says so out loud.
    """
    leaked = [name for name in env_variable_names() if os.environ.get(name)]
    assert leaked == [], f"host environment variables reached the tests: {leaked}"


def test_an_implicit_load_finds_no_credentials():
    """The same guarantee seen from the other side: through ``Config.load`` itself."""
    config = Config.load()
    assert config.openalex.api_key is None
    assert config.contact_email is None
    assert config.advisor.api_key is None


def test_the_isolated_set_is_derived_from_the_loader_not_hand_listed():
    """A new environment variable must not be able to slip past the isolation."""
    names = env_variable_names()
    assert "HARVESTER_OPENALEX_API_KEY" in names
    assert "HARVESTER_CONTACT_EMAIL" in names
    assert "ANTHROPIC_API_KEY" in names          # the advisor vendor's own variable
    assert "HARVESTER_CONFIG" in names           # names the config file itself


def test_defaults_are_usable_without_any_input():
    config = Config.load(env={})
    assert config.xml_policy == "preferred"
    assert config.openalex.per_page == 200
    assert config.downloads.max_download_size_bytes > 0
    assert config.retry.max_attempts >= 1


def test_precedence_cli_over_env_over_file_over_defaults(tmp_path: Path):
    path = write_config(
        tmp_path,
        {
            "storage_root": "from-file",
            "xml_policy": "required",
            "openalex": {"api_key": "file-key", "per_page": 10},
        },
    )
    env = {"HARVESTER_STORAGE_ROOT": "from-env", "HARVESTER_OPENALEX_API_KEY": "env-key"}
    config = Config.load(
        config_path=path, env=env, overrides={"storage_root": "from-cli"}
    )
    assert str(config.storage_root) == "from-cli"       # CLI beats env
    assert config.openalex.api_key == "env-key"          # env beats file
    assert config.xml_policy == "required"               # file beats default
    assert config.openalex.per_page == 10


def test_env_variables_are_coerced_to_declared_types():
    config = Config.load(
        env={
            "HARVESTER_OPENALEX_CONCURRENCY": "7",
            "HARVESTER_OPENALEX_RPS": "2.5",
            "HARVESTER_OPENALEX_ALLOW_KEYLESS": "true",
            "HARVESTER_MAX_DOWNLOAD_SIZE_BYTES": "1024",
            "HARVESTER_STORAGE_ROOT": "/data/corpus",
        }
    )
    assert config.openalex.concurrency == 7
    assert config.openalex.requests_per_second == 2.5
    assert config.openalex.allow_keyless is True
    assert config.downloads.max_download_size_bytes == 1024
    assert isinstance(config.storage_root, Path)


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_boolean_true_forms(value):
    assert Config.load(env={"HARVESTER_OPENALEX_ALLOW_KEYLESS": value}).openalex.allow_keyless


@pytest.mark.parametrize("value", ["0", "false", "NO", "off"])
def test_boolean_false_forms(value):
    assert not Config.load(env={"HARVESTER_OPENALEX_ALLOW_KEYLESS": value}).openalex.allow_keyless


def test_unknown_keys_are_rejected_rather_than_ignored(tmp_path: Path):
    path = write_config(tmp_path, {"nonsense": 1})
    with pytest.raises(ConfigurationError, match="unknown configuration key"):
        Config.load(config_path=path, env={})


def test_unknown_section_keys_are_rejected(tmp_path: Path):
    path = write_config(tmp_path, {"openalex": {"nonsense": 1}})
    with pytest.raises(ConfigurationError, match="openalex.nonsense"):
        Config.load(config_path=path, env={})


def test_malformed_config_file_is_reported(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="invalid JSON"):
        Config.load(config_path=path, env={})


def test_missing_config_file_is_reported(tmp_path: Path):
    with pytest.raises(ConfigurationError, match="not found"):
        Config.load(config_path=tmp_path / "absent.json", env={})


# ---------------------------------------------------------------- validation


def test_openalex_requires_a_key_for_production():
    """SPEC_PATCH section 2: normal production operation uses configured credentials."""
    config = Config.load(env={})
    config.contact_email = "operator@example.org"
    with pytest.raises(ConfigurationError, match="requires an API key"):
        config.validate()


def test_keyless_openalex_must_be_deliberately_enabled():
    config = Config.load(env={})
    config.contact_email = "operator@example.org"
    config.openalex.allow_keyless = True
    config.validate()  # does not raise


def test_unpaywall_requires_a_contact_email():
    config = Config.load(env={"HARVESTER_OPENALEX_API_KEY": "k"})
    with pytest.raises(ConfigurationError, match="contact email"):
        config.validate()


def test_unpaywall_can_be_disabled_instead():
    config = Config.load(env={"HARVESTER_OPENALEX_API_KEY": "k"})
    config.unpaywall.enabled = False
    config.validate()


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda c: setattr(c, "xml_policy", "maybe"), "xml_policy"),
        (lambda c: setattr(c, "log_format", "yaml"), "log_format"),
        (lambda c: setattr(c.openalex, "concurrency", 0), "concurrency"),
        (lambda c: setattr(c.openalex, "requests_per_second", 0), "requests_per_second"),
        (lambda c: setattr(c.openalex, "per_page", 500), "per_page"),
        (lambda c: setattr(c.openalex, "daily_credit_ceiling", 0), "daily_credit_ceiling"),
        (lambda c: setattr(c.downloads, "max_download_size_bytes", 0), "max_download_size_bytes"),
        (lambda c: setattr(c.retry, "max_attempts", 0), "max_attempts"),
        (lambda c: setattr(c.retry, "jitter_ratio", 3), "jitter_ratio"),
    ],
)
def test_invalid_values_are_rejected(mutate, message):
    config = Config.load(env={"HARVESTER_OPENALEX_API_KEY": "k"})
    config.contact_email = "operator@example.org"
    mutate(config)
    with pytest.raises(ConfigurationError, match=message):
        config.validate()


# ------------------------------------------------------------------- AC-017


def test_ac017_secrets_are_redacted_from_the_config_snapshot():
    """AC-017: credentials and contact addresses never reach reports, state or logs."""
    config = Config.load(
        env={
            "HARVESTER_OPENALEX_API_KEY": "super-secret-key",
            "HARVESTER_CONTACT_EMAIL": "operator@example.org",
        }
    )
    snapshot = config.redacted_dict()
    serialized = json.dumps(snapshot)
    assert "super-secret-key" not in serialized
    assert "operator@example.org" not in serialized
    assert snapshot["openalex"]["api_key"] == "***REDACTED***"
    assert snapshot["contact_email"] == "***REDACTED***"


def test_user_agent_identifies_the_client():
    """MASTER_SPEC section 28: requests must carry a meaningful User-Agent."""
    config = Config.load(env={"HARVESTER_CONTACT_EMAIL": "operator@example.org"})
    agent = config.effective_user_agent()
    assert "OpenAccessAPIHarvester" in agent
    assert "operator@example.org" in agent

    anonymous = Config.load(env={})
    assert "mailto" not in anonymous.effective_user_agent()


def test_explicit_user_agent_wins():
    config = Config.load(env={"HARVESTER_USER_AGENT": "CustomAgent/9"})
    assert config.effective_user_agent() == "CustomAgent/9"
