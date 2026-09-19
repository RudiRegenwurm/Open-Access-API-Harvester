"""CLI runtime paths must survive the web-server handoff and settings reload."""
import threading
from pathlib import Path

import pytest

from harvester.cli import main
from harvester.webui.server import ControlCenterServer
from conftest import Client


@pytest.mark.parametrize("conflicting_paths", [False, True])
def test_serve_runtime_paths_from_protected_cwd(tmp_path, monkeypatch, conflicting_paths):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    config_path = work / "harvester.json"
    config_path.write_text(
        '{"state_db": "state/ignored.sqlite3"}' if conflicting_paths else "{}",
        encoding="utf-8",
    )
    monkeypatch.chdir(foreign)
    mkdir = Path.mkdir

    def protected_mkdir(path, *args, **kwargs):
        if path.resolve().is_relative_to(foreign):
            raise PermissionError(13, "[WinError 5] Zugriff verweigert", str(path))
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", protected_mkdir)
    original_serve = ControlCenterServer.serve_forever

    def exercise(server, **kwargs):
        thread = threading.Thread(target=original_serve, args=(server,),
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        client = Client("http://127.0.0.1:%s" % server.server_address[1])
        try:
            result = client.get("/api/runs")
            assert client.status == 200, result
            assert result == {"runs": []}
            client.get("/api/dashboard")
            assert client.status == 200
            # A settings save must not silently switch back to the default DB.
            result = client.put("/api/settings", {"updates": {"log_level": "INFO", "unpaywall.enabled": False}})
            assert client.status == 200, result
            result = client.get("/api/runs")
            assert client.status == 200, result
            for key, value in paths.items():
                assert getattr(server.context.config, key) == value
            assert paths["state_db"].is_file()
            assert list(foreign.iterdir()) == []
        finally:
            server.shutdown()
            thread.join(timeout=5)

    monkeypatch.setattr(ControlCenterServer, "serve_forever", exercise)
    paths = {"state_db": work / "state" / "harvester.sqlite3",
             "storage_root": work / "corpus", "reports_dir": work / "reports"}
    args = ["serve", "--config", str(config_path), "--port", "0", "--no-browser"]
    for key, value in paths.items():
        args.extend(["--" + key.replace("_", "-"), str(value)])
    assert main(args) == 0
