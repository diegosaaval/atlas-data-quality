"""Lanzador de ATLAS: prepara todo y abre el navegador. Lo usan «Iniciar ATLAS.bat/.command» y start.sh.

    python run.py                 # instala si hace falta, arranca y abre el navegador
    python run.py --test          # corre las pruebas
    python run.py --reinstall     # rehace el entorno
    python run.py --sin-acceso    # no crear el acceso directo en el escritorio

Solo usa la librería estándar, así funciona con un Python recién instalado.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
MARKER = VENV / ".atlas-installed"
SHORTCUT_MARKER = VENV / ".atlas-acceso"
WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"
VENV_PY = VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")


def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"\n  Algo falló: {msg}\n", flush=True)
    sys.exit(1)


# ------------------------------------------------------------------ entorno
def deps_fingerprint() -> str:
    return hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest()


def venv_healthy() -> bool:
    """El intérprete del entorno corre y tiene pip."""
    if not VENV_PY.exists():
        return False
    probe = subprocess.run([str(VENV_PY), "-c", "import pip"], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return probe.returncode == 0


def ensure_env(reinstall: bool) -> None:
    if sys.version_info < (3, 11):  # noqa: UP036 - corre con el Python que tenga la persona
        fail(f"ATLAS necesita Python 3.11 o más reciente (tienes {sys.version.split()[0]}). "
             "Descárgalo en https://www.python.org/downloads/")

    if reinstall and VENV.exists():
        say("Borrando el entorno anterior…")
        shutil.rmtree(VENV)

    if VENV.exists() and not venv_healthy():
        # Creado por otra herramienta sin pip, copiado de otro equipo o interrumpido.
        say("El entorno está incompleto, reparándolo…")
        subprocess.run([str(VENV_PY), "-m", "ensurepip", "--upgrade"], cwd=ROOT,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if not venv_healthy():
            say("No se pudo reparar, lo creo de nuevo…")
            shutil.rmtree(VENV, ignore_errors=True)

    if not VENV_PY.exists():
        say(f"Preparando ATLAS por primera vez (Python {sys.version.split()[0]})…")
        venv.create(VENV, with_pip=True)
        if not venv_healthy():
            fail("no se pudo crear el entorno con pip. En Debian/Ubuntu instala 'python3-venv'; "
                 "si no, reinstala Python desde https://www.python.org/downloads/")

    if MARKER.exists() and MARKER.read_text() == deps_fingerprint():
        return

    say("Instalando componentes, solo la primera vez (necesita internet, ~1 minuto)…")
    pip = [str(VENV_PY), "-m", "pip", "--disable-pip-version-check"]
    subprocess.run([*pip, "install", "-q", "--upgrade", "pip"], cwd=ROOT, check=False)
    if subprocess.run([*pip, "install", "-q", "-e", ".[ai,dev]"], cwd=ROOT).returncode != 0:
        fail("no se pudieron instalar los componentes. Revisa tu conexión y vuelve a abrir "
             "(o usa --reinstall).")
    MARKER.write_text(deps_fingerprint())


# --------------------------------------------------------- acceso directo
def create_shortcut() -> str | None:
    """Acceso directo «ATLAS» con ícono en el escritorio (Windows .lnk, Mac .app, Linux .desktop)."""
    desktop = Path.home() / "Desktop"
    if not desktop.is_dir():
        return None
    try:
        if WINDOWS:
            bat, ico = ROOT / "Iniciar ATLAS.bat", ROOT / "web" / "icon.ico"
            q = lambda p: str(p).replace("'", "''")  # noqa: E731 - comillas de PowerShell
            ps = ("$w = New-Object -ComObject WScript.Shell; "
                  "$s = $w.CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'), 'ATLAS.lnk')); "
                  f"$s.TargetPath = '{q(bat)}'; $s.WorkingDirectory = '{q(ROOT)}'; $s.IconLocation = '{q(ico)}'; "
                  "$s.Description = 'ATLAS · Monitor de calidad de datos'; $s.Save()")
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return str(desktop / "ATLAS.lnk")
        if MAC:
            app = desktop / "ATLAS.app"
            (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
            (app / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / "web" / "icon.icns", app / "Contents" / "Resources" / "icon.icns")
            script = app / "Contents" / "MacOS" / "ATLAS"
            script.write_text(f"#!/bin/bash\nopen -a Terminal {shlex.quote(str(ROOT / 'Iniciar ATLAS.command'))}\n")
            script.chmod(0o755)
            with open(app / "Contents" / "Info.plist", "wb") as fh:
                plistlib.dump({
                    "CFBundleName": "ATLAS", "CFBundleDisplayName": "ATLAS",
                    "CFBundleIdentifier": "io.github.diegosaaval.atlas", "CFBundleExecutable": "ATLAS",
                    "CFBundleIconFile": "icon", "CFBundlePackageType": "APPL", "CFBundleVersion": "1",
                    "LSMinimumSystemVersion": "10.13",
                }, fh)
            os.utime(app)  # que el Finder refresque el ícono
            return str(app)
        entry = desktop / "atlas.desktop"
        entry.write_text(
            "[Desktop Entry]\nType=Application\nName=ATLAS\nComment=Monitor de calidad de datos\n"
            f"Exec=bash -c 'cd {shlex.quote(str(ROOT))} && ./start.sh'\n"
            f"Icon={ROOT / 'web' / 'icon-512.png'}\nTerminal=true\n")
        entry.chmod(0o755)
        return str(entry)
    except Exception as exc:  # nunca impedir que ATLAS arranque por un acceso directo
        say(f"(No pude crear el acceso directo: {exc})")
        return None


def maybe_shortcut(skip: bool) -> None:
    mac_app = Path.home() / "Desktop" / "ATLAS.app"
    if skip:
        return
    if not SHORTCUT_MARKER.exists():
        made = create_shortcut()
        SHORTCUT_MARKER.write_text("ok")
        if made:
            say("Creé un acceso directo «ATLAS» en tu escritorio, con su ícono.")
    elif MAC and mac_app.exists():
        create_shortcut()  # si moviste la carpeta del proyecto, el acceso se actualiza solo


# ------------------------------------------------------------------- red
def lan_ip() -> str:
    """IP de este equipo en la red local (no envía nada a internet)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def atlas_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=1) as r:
            return "copilot" in json.loads(r.read())
    except (OSError, ValueError):
        return False


def free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 50):
        if not port_in_use(port):
            return port
    fail(f"no hay puertos libres entre {preferred} y {preferred + 49}.")
    return preferred


def wait_until_ready(url: str, proc: subprocess.Popen, timeout: float = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=1):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def serve(port: int, open_browser: bool) -> None:
    if atlas_running(port):
        say("ATLAS ya está abierto. Abriendo el navegador…")
        webbrowser.open(f"http://localhost:{port}/")
        return
    port = free_port(port)
    local, lan = f"http://localhost:{port}", f"http://{lan_ip()}:{port}"
    say("Encendiendo ATLAS…")
    proc = subprocess.Popen(
        [str(VENV_PY), "-m", "uvicorn", "atlas.api:app", "--host", "0.0.0.0", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT,
    )
    try:
        if not wait_until_ready(local, proc):
            fail("el servidor no arrancó. Revisa los mensajes de arriba.")
        print()
        print("  ================================================")
        print("    ATLAS · Monitor de calidad de datos")
        print("  ================================================")
        print()
        print(f"    En este equipo:        {local}")
        print(f"    Desde tu celular:      {lan}   (misma red Wi-Fi)")
        print(f"    Documentación de API:  {local}/docs")
        print()
        print("    NO cierres esta ventana: si la cierras, ATLAS se apaga.")
        print("    (Puedes minimizarla.)")
        print()
        if open_browser:
            threading.Timer(0.3, webbrowser.open, args=(local + "/",)).start()
        proc.wait()
    except KeyboardInterrupt:
        say("Apagando ATLAS…")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description="Lanzador de ATLAS")
    parser.add_argument("--port", type=int, default=int(os.getenv("ATLAS_PORT", "8000")))
    parser.add_argument("--no-browser", action="store_true", help="no abrir el navegador")
    parser.add_argument("--test", action="store_true", help="correr las pruebas en vez del servidor")
    parser.add_argument("--reinstall", action="store_true", help="rehacer el entorno")
    parser.add_argument("--sin-acceso", action="store_true", help="no crear el acceso directo en el escritorio")
    args = parser.parse_args()

    sys.stdout.reconfigure(line_buffering=True)  # los mensajes salen en el acto, también en Windows
    os.chdir(ROOT)
    ensure_env(args.reinstall)
    if args.test:
        sys.exit(subprocess.run([str(VENV_PY), "-m", "pytest"], cwd=ROOT).returncode)
    maybe_shortcut(args.sin_acceso)
    serve(args.port, not args.no_browser)


if __name__ == "__main__":
    main()
