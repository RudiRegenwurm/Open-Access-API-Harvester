"""Offline verifier: Python standard library only, no Notanda or MCP imports."""

import hashlib
import json
from pathlib import Path
import sys


def verify(folder):
    manifest = json.loads((folder / "manifest.json").read_bytes())
    assert manifest["schema_version"] == 1
    assert set(manifest["files"]) == {
        "request.json",
        "provider.json",
        "response.json",
    }
    for name, expected in manifest["files"].items():
        raw = (folder / name).read_bytes()
        assert len(raw) == expected["bytes"], name + ": size mismatch"
        assert hashlib.sha256(raw).hexdigest() == expected["sha256"], (
            name + ": SHA-256 mismatch"
        )
    request = json.loads((folder / "request.json").read_bytes())
    response = json.loads((folder / "response.json").read_bytes())
    assert request["evidence_id"] == response["evidence_id"] == folder.name
    return response


if __name__ == "__main__":
    try:
        response = verify(Path(sys.argv[1]))
    except (AssertionError, OSError, ValueError, KeyError, IndexError) as exc:
        print("FAIL:", type(exc).__name__)
        sys.exit(1)
    print("PASS:", response["evidence_id"], "outcome:", response["status"])
