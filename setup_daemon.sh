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
$PIP install --upgrade pip
TARGET_DIR="$SCRIPT_DIR/.deps"
mkdir -p "$TARGET_DIR"
if [ "$NO_UI" -eq 1 ]; then
  $PIP install --target "$TARGET_DIR" playwright requests beautifulsoup4 lxml
else
  $PIP install --target "$TARGET_DIR" playwright requests beautifulsoup4 lxml PySide6
fi

echo "==> Installing Playwright browsers"
PYTHONPATH="$TARGET_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -m playwright install

if [ "$WITH_DEPS" -eq 1 ] && command_exists apt-get; then
  echo "==> Installing Playwright system deps (Linux)"
  PYTHONPATH="$TARGET_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -m playwright install-deps
fi

echo "==> Done"
