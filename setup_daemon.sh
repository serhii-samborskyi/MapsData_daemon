#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NO_UI=0
WITH_DEPS=0
for arg in "$@"; do
  case "$arg" in
    --no-ui) NO_UI=1 ;;
    --with-deps) WITH_DEPS=1 ;;
  esac
done

command_exists() { command -v "$1" >/dev/null 2>&1; }

echo "==> Checking python3"
if ! command_exists python3; then
  echo "python3 not found. Attempting to install..."
  if command_exists brew; then
    brew install python
  elif command_exists apt-get; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv python3-pip
  elif command_exists dnf; then
    sudo dnf install -y python3 python3-venv python3-pip
  elif command_exists yum; then
    sudo yum install -y python3 python3-venv python3-pip
  elif command_exists pacman; then
    sudo pacman -Sy --noconfirm python python-pip
  else
    echo "No supported package manager found. Install python3 manually."
    exit 1
  fi
fi

if ! command_exists pip3; then
  echo "pip3 not found. Trying ensurepip..."
  python3 -m ensurepip --upgrade || true
fi

PIP="python3 -m pip"
if ! $PIP --version >/dev/null 2>&1; then
  if command_exists pip3; then
    PIP="pip3"
  else
    echo "pip is not available. Install pip and retry."
    exit 1
  fi
fi

echo "==> Installing Python packages"
TARGET_DIR="$SCRIPT_DIR/.deps"
mkdir -p "$TARGET_DIR"
if [ "$NO_UI" -eq 1 ]; then
  $PIP install --upgrade --target "$TARGET_DIR" camoufox playwright requests beautifulsoup4 lxml scrapy
else
  $PIP install --upgrade --target "$TARGET_DIR" camoufox playwright requests beautifulsoup4 lxml scrapy PySide6
fi

echo "==> Fetching Camoufox browser binaries"
PYTHONPATH="$TARGET_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -m camoufox fetch

if [ "$WITH_DEPS" -eq 1 ] && command_exists apt-get; then
  echo "==> Installing runtime deps for daemon workers (Linux)"
  sudo apt-get update
  sudo apt-get install -y xvfb libnss3 libgtk-3-0 libx11-xcb1 libxcomposite1 libxdamage1 libxi6 libxtst6 libasound2t64 || true
fi

echo "==> Done"
