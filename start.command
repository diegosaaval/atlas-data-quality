#!/bin/bash
# ATLAS ONE - doble clic (macOS) o ./start.command (Linux/macOS) para instalar todo y abrir la plataforma.
cd "$(dirname "$0")" || exit 1

PY=""
for cand in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done

if [ -z "$PY" ]; then
  echo
  echo "  No se encontro Python 3.11 o superior."
  if command -v brew >/dev/null 2>&1; then
    read -r -p "  Instalar Python 3.12 con Homebrew? [s/N] " ans
    if [[ "$ans" =~ ^[sSyY]$ ]]; then brew install python@3.12 && PY="$(brew --prefix)/bin/python3.12"; fi
  fi
  if [ -z "$PY" ]; then
    echo "  Descargalo de https://www.python.org/downloads/ y vuelve a abrir start.command"
    open "https://www.python.org/downloads/" 2>/dev/null || true
    read -r -p "  Enter para cerrar…" _
    exit 1
  fi
fi

"$PY" run.py "$@"
status=$?
[ $status -ne 0 ] && read -r -p "  Enter para cerrar…" _
exit $status
