#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${T3_GEMINI_BRIDGE_ROOT:-$HOME/.local/share/t3-gemini-bridge}"
BIN_DIR="$HOME/.local/bin"
LABEL="${T3_GEMINI_BRIDGE_LABEL:-io.t3-gemini-bridge.daemon}"
INSTALL_SERVICE=false

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Install the T3 Code <-> Gemini Desktop bridge scripts and CLI wrapper locally
without overwriting any existing pairs.json, bridge_config.json, ledger.json,
or queue directories.

Options:
  --root <dir>        Bridge runtime root (default: ~/.local/share/t3-gemini-bridge)
  --bin-dir <dir>     CLI wrapper directory (default: ~/.local/bin)
  --label <label>     LaunchAgent label (default: io.t3-gemini-bridge.daemon)
  --install-service   Also install and start the macOS LaunchAgent daemon
  -h, --help          Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)
      [[ $# -ge 2 ]] || { echo "Error: --root requires an argument" >&2; exit 2; }
      ROOT="$2"
      shift 2
      ;;
    --bin-dir)
      [[ $# -ge 2 ]] || { echo "Error: --bin-dir requires an argument" >&2; exit 2; }
      BIN_DIR="$2"
      shift 2
      ;;
    --label)
      [[ $# -ge 2 ]] || { echo "Error: --label requires an argument" >&2; exit 2; }
      LABEL="$2"
      shift 2
      ;;
    --install-service)
      INSTALL_SERVICE=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Error: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! "$LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || [[ "$LABEL" == *".."* ]]; then
  echo "Error: invalid LaunchAgent label: $LABEL" >&2
  exit 2
fi

if [[ ! -f "$SCRIPT_DIR/t3_gemini_bridge.py" || ! -f "$SCRIPT_DIR/bridge_ax.py" ]]; then
  echo "Error: bundled bridge scripts not found in $SCRIPT_DIR" >&2
  exit 1
fi

PY_BIN="$(command -v python3 || true)"
if [[ -z "$PY_BIN" && -x /usr/bin/python3 ]]; then
  PY_BIN="/usr/bin/python3"
fi
if [[ -z "$PY_BIN" ]]; then
  echo "Error: python3 (3.9+) is required but was not found" >&2
  exit 1
fi

"$PY_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || {
  echo "Error: Python 3.9+ is required (found $("$PY_BIN" --version 2>&1))" >&2
  exit 1
}

mkdir -p "$ROOT" "$BIN_DIR"
chmod 700 "$ROOT"

cp "$SCRIPT_DIR/t3_gemini_bridge.py" "$ROOT/t3_gemini_bridge.py"
cp "$SCRIPT_DIR/bridge_ax.py" "$ROOT/bridge_ax.py"
chmod 755 "$ROOT/t3_gemini_bridge.py" "$ROOT/bridge_ax.py"

WRAPPER_TMP="$BIN_DIR/.t3-gemini-bridge.tmp.$$"
cat > "$WRAPPER_TMP" <<EOF
#!/bin/sh
export T3_GEMINI_BRIDGE_ROOT="\${T3_GEMINI_BRIDGE_ROOT:-$ROOT}"
export T3_GEMINI_BRIDGE_LABEL="\${T3_GEMINI_BRIDGE_LABEL:-$LABEL}"
exec $PY_BIN -B "$ROOT/t3_gemini_bridge.py" "\$@"
EOF
chmod 755 "$WRAPPER_TMP"
mv "$WRAPPER_TMP" "$BIN_DIR/t3-gemini-bridge"

# Initialize directories/config/ledger safely without touching existing state files
T3_GEMINI_BRIDGE_ROOT="$ROOT" T3_GEMINI_BRIDGE_LABEL="$LABEL" \
  "$BIN_DIR/t3-gemini-bridge" status --json >/dev/null

RESOLVED_TCC_PY="$("$PY_BIN" -B -c "import sys; sys.path.insert(0, '$ROOT'); import t3_gemini_bridge as tb; print(tb.resolve_launchagent_python())")"

echo "Installed t3-gemini-bridge:"
echo "  Bridge root : $ROOT"
echo "  CLI wrapper : $BIN_DIR/t3-gemini-bridge"
echo "  LaunchAgent : $LABEL"
echo "  TCC Python  : $RESOLVED_TCC_PY"

if [[ "$(uname -s)" == "Darwin" ]]; then
  if [[ ! -d "/Applications/Gemini.app" && ! -d "$HOME/Applications/Gemini.app" ]]; then
    echo "  [warn] Gemini.app not found in /Applications or ~/Applications"
  fi
fi

T3_HOME="${T3_GEMINI_BRIDGE_T3_HOME:-$HOME/.t3}"
if [[ -n "${T3_GEMINI_BRIDGE_CLI:-}" && -x "${T3_GEMINI_BRIDGE_CLI}" ]]; then
  echo "  T3 CLI      : ${T3_GEMINI_BRIDGE_CLI}"
elif [[ -x "$T3_HOME/bin/t3" ]]; then
  echo "  T3 CLI      : $T3_HOME/bin/t3"
elif command -v t3 >/dev/null 2>&1; then
  echo "  T3 CLI      : $(command -v t3)"
else
  echo "  [warn] t3 CLI not found at $T3_HOME/bin/t3 or on PATH (install T3 Code CLI or set T3_GEMINI_BRIDGE_CLI)"
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo ""
    echo "Hint: $BIN_DIR is not in your PATH. Add it in your shell profile:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac

if [[ "$INSTALL_SERVICE" == "true" ]]; then
  echo ""
  echo "Installing LaunchAgent service..."
  "$BIN_DIR/t3-gemini-bridge" service install
fi

echo ""
echo "Next steps:"
echo "  1. In macOS System Settings -> Privacy & Security -> Accessibility (and Full Disk Access if workspaces live in Desktop/Documents), enable:"
echo "       $RESOLVED_TCC_PY"
if [[ "$INSTALL_SERVICE" != "true" ]]; then
  echo "  2. Install and start the LaunchAgent daemon:"
  echo "       $BIN_DIR/t3-gemini-bridge service install"
  echo "  3. Verify health:"
  echo "       $BIN_DIR/t3-gemini-bridge doctor --json"
else
  echo "  2. Verify health:"
  echo "       $BIN_DIR/t3-gemini-bridge doctor --json"
fi
