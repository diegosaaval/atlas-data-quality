import socket

import run


def test_lan_ip_is_an_ipv4_address():
    parts = run.lan_ip().split(".")
    assert len(parts) == 4 and all(p.isdigit() for p in parts)


def test_free_port_skips_busy_ports():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        busy = s.getsockname()[1]
        assert run.free_port(busy) != busy


def test_atlas_running_is_false_when_nothing_listens():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert run.atlas_running(port) is False


def test_mac_shortcut_is_a_valid_app_bundle(tmp_path, monkeypatch):
    import plistlib

    monkeypatch.setattr(run, "WINDOWS", False)
    monkeypatch.setattr(run, "MAC", True)
    monkeypatch.setattr(run.Path, "home", lambda: tmp_path)
    (tmp_path / "Desktop").mkdir()
    app = run.create_shortcut()
    assert app and app.endswith("ATLAS.app")
    info = plistlib.loads((tmp_path / "Desktop/ATLAS.app/Contents/Info.plist").read_bytes())
    assert info["CFBundleIconFile"] == "icon"
    assert (tmp_path / "Desktop/ATLAS.app/Contents/Resources/icon.icns").stat().st_size > 1000
    assert "Iniciar ATLAS.command" in (tmp_path / "Desktop/ATLAS.app/Contents/MacOS/ATLAS").read_text()
