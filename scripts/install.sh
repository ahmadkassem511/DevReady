#!/usr/bin/env sh
# =============================================================================
# DevReady one-time installer for macOS & Linux (the "easy app" path)
# -----------------------------------------------------------------------------
# This is the only time a non-technical user touches a terminal. It:
#   1. installs `uv` if missing — a single static binary that needs NO existing
#      Python (it downloads Python for us; no admin rights, no PATH surgery),
#   2. installs DevReady (with the web GUI) as an isolated tool,
#   3. creates a desktop launcher so future use is just a double-click,
#   4. launches the browser GUI.
#
# Usage:  sh install.sh
# =============================================================================
set -e

REPO="https://github.com/ahmadkassem511/DevReady"

echo "DevReady installer"
echo "=================="

# 1) Ensure uv -------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "-> Installing uv (one-time, no admin needed)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# Make uv visible in THIS shell session regardless of where it landed.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# 2) Install DevReady with the GUI extra -----------------------------------
echo "-> Installing DevReady..."
uv tool install --force "devready[ui] @ git+$REPO"

DEVREADY="$(command -v devready || echo "$HOME/.local/bin/devready")"

# 3) Create a clickable launcher so the terminal is never needed again -----
OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
  LAUNCHER="$HOME/Desktop/DevReady.command"
  printf '#!/bin/sh\nexec "%s" ui\n' "$DEVREADY" > "$LAUNCHER"
  chmod +x "$LAUNCHER"
  echo "-> Created a 'DevReady' launcher on your Desktop (double-click it next time)."
else
  APPS="$HOME/.local/share/applications"
  mkdir -p "$APPS"
  cat > "$APPS/devready.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DevReady
Comment=Set up and run any project with a click
Exec=$DEVREADY ui
Terminal=true
Categories=Development;
EOF
  cp "$APPS/devready.desktop" "$HOME/Desktop/DevReady.desktop" 2>/dev/null || true
  chmod +x "$HOME/Desktop/DevReady.desktop" 2>/dev/null || true
  echo "-> Added 'DevReady' to your applications menu (use it next time)."
fi

# 4) Launch now ------------------------------------------------------------
echo "-> Starting DevReady — your browser will open shortly."
exec "$DEVREADY" ui
