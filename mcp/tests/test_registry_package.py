"""Check the installable package/Registry contract, not just source declarations."""
import importlib.metadata
import json
from pathlib import Path

from packaging.version import Version


def test_installed_beta_package_matches_registry_and_ownership_marker():
    package = importlib.metadata.distribution("notanda-mcp")
    manifest = json.loads((Path(__file__).parents[1] / "server.json").read_text())
    pypi = manifest["packages"][0]
    assert package.version == pypi["version"]
    assert Version(package.version).is_prerelease
    assert Version(manifest["version"]) == Version(package.version)
    assert "Development Status :: 4 - Beta" in package.metadata.get_all("Classifier")
    assert "mcp-name: " + manifest["name"] in package.metadata.get_payload()
    assert "Thomas" in package.metadata.get_payload()
    assert "pending" in manifest["description"].lower()
    assert pypi["transport"]["type"] == "stdio"
    assert {e.name for e in package.entry_points if e.group == "console_scripts"} == {"notanda-mcp"}
    assert pypi["packageArguments"][0]["name"] == "--evidence-dir"
    assert pypi["packageArguments"][0]["isRequired"]
    key = pypi["environmentVariables"][0]
    assert key["name"] == "HARVESTER_OPENALEX_API_KEY"
    assert key["isRequired"] and key["isSecret"] and "value" not in key
