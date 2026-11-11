#!/usr/bin/env bash
# Start the source backend and Tauri shell on native macOS.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND="$ROOT/frontend"
TAURI_DIR="$ROOT/src-tauri"
WORKER_DIR="$ROOT/native/macos-stt-worker"
PORT=38420

NO_TAURI=0
NO_HOTKEYS=0
BOOTSTRAP_ONLY=0
DATA_DIR=""

usage() {
  cat <<'EOF'
Usage: scripts/run_dev.sh [options]

Options:
  --no-tauri                  Start the backend and Vite without the native shell
  --no-hotkeys                Disable global hotkeys in the source backend
  --bootstrap-only            Install and stage dependencies, then exit
  --experimental              Enable experimental development surfaces
  --data-dir PATH             Override isolated development state
  -h, --help                  Show this help
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-tauri) NO_TAURI=1 ;;
    --no-hotkeys) NO_HOTKEYS=1 ;;
    --bootstrap-only) BOOTSTRAP_ONLY=1 ;;
    --experimental) export WHISPERSTREAM_EXPERIMENTAL=1 ;;
    --data-dir)
      shift
      [ "$#" -gt 0 ] || { echo "--data-dir requires a path" >&2; exit 2; }
      DATA_DIR="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ "$(uname -s)" != "Darwin" ]; then
  echo "scripts/run_dev.sh requires macOS." >&2
  exit 1
fi
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
  arm64) NODE_ARCH=arm64; TARGET_TRIPLE=aarch64-apple-darwin ;;
  x86_64) NODE_ARCH=x64; TARGET_TRIPLE=x86_64-apple-darwin ;;
  *) echo "Unsupported macOS architecture: $HOST_ARCH" >&2; exit 1 ;;
esac
export WHISPERSTREAM_NODE_ARCH="$NODE_ARCH"

for tool in cargo curl node npm uv; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Required command is missing: $tool" >&2
    exit 1
  fi
done

if [ "$(node -p 'process.arch')" != "$NODE_ARCH" ]; then
  echo "Node must run natively for $HOST_ARCH; install the matching Node build." >&2
  exit 1
fi

export WHISPERSTREAM_AGENT_PLUGIN_VARIANT=dev
if [ -n "$DATA_DIR" ]; then
  export WHISPERSTREAM_DATA_DIR="$DATA_DIR"
elif [ -z "${WHISPERSTREAM_DATA_DIR:-}" ]; then
  export WHISPERSTREAM_DATA_DIR="$HOME/Library/Application Support/Whisperstream-dev"
fi

# Never use installed application state for development.
DATA_PATH="$WHISPERSTREAM_DATA_DIR" python3 <<'PYDATA'
import os
from pathlib import Path
candidate = Path(os.environ["DATA_PATH"]).expanduser().resolve()
installed = (Path.home() / "Library/Application Support/Whisperstream").resolve()
if candidate == installed or installed in candidate.parents:
    raise SystemExit("Use a separate development data directory.")
PYDATA

native_node_bindings_valid() {
  FRONTEND_DIR="$FRONTEND" node <<'JS'
const fs = require("node:fs");
const path = require("node:path");

const root = process.env.FRONTEND_DIR;
const lock = JSON.parse(fs.readFileSync(path.join(root, "package-lock.json"), "utf8"));
const arch = process.env.WHISPERSTREAM_NODE_ARCH;
const packages = [
  `@tauri-apps/cli-darwin-${arch}`,
  `@rollup/rollup-darwin-${arch}`,
  `@esbuild/darwin-${arch}`,
  `@tailwindcss/oxide-darwin-${arch}`,
  `lightningcss-darwin-${arch}`,
];
let valid = true;
for (const name of packages) {
  const expected = lock.packages?.[`node_modules/${name}`]?.version;
  const installedPath = path.join(root, "node_modules", name, "package.json");
  let installed;
  if (fs.existsSync(installedPath)) {
    installed = JSON.parse(fs.readFileSync(installedPath, "utf8")).version;
  }
  if (!expected || installed !== expected) {
    console.error(`[mac-dev] native Node binding mismatch package=${name} expected=${expected || "missing-lock"} installed=${installed || "missing"}`);
    valid = false;
  }
}
process.exit(valid ? 0 : 1);
JS
}

install_native_node_bindings() {
  local specs
  specs="$(FRONTEND_DIR="$FRONTEND" node <<'JS'
const fs = require("node:fs");
const path = require("node:path");
const root = process.env.FRONTEND_DIR;
const lock = JSON.parse(fs.readFileSync(path.join(root, "package-lock.json"), "utf8"));
const arch = process.env.WHISPERSTREAM_NODE_ARCH;
const packages = [
  `@tauri-apps/cli-darwin-${arch}`,
  `@rollup/rollup-darwin-${arch}`,
  `@esbuild/darwin-${arch}`,
  `@tailwindcss/oxide-darwin-${arch}`,
  `lightningcss-darwin-${arch}`,
];
for (const name of packages) {
  const version = lock.packages?.[`node_modules/${name}`]?.version;
  if (!version) throw new Error(`native package missing from lockfile: ${name}`);
  console.log(`${name}@${version}`);
}
JS
)"
  echo "[mac-dev] repairing pinned $HOST_ARCH Node bindings"
  # Package specs contain no whitespace. Intentional splitting passes each
  # lock-derived name@version as one npm argument on macOS Bash 3.2.
  # shellcheck disable=SC2086
  (cd "$FRONTEND" && npm install --no-save --package-lock=false --os=darwin --cpu="$NODE_ARCH" $specs)
}

if [ ! -f "$FRONTEND/node_modules/vite/package.json" ] || [ ! -f "$FRONTEND/node_modules/@tauri-apps/cli/package.json" ]; then
  echo "[mac-dev] frontend dependencies missing, running npm ci"
  (cd "$FRONTEND" && npm ci --include=optional --os=darwin --cpu="$NODE_ARCH")
fi
if ! native_node_bindings_valid; then
  install_native_node_bindings
  native_node_bindings_valid
fi

# The locked official sherpa-onnx wheels serve every sherpa STT family and
# Supertonic, so an ordinary `uv run` leaves this environment unchanged.
# Honor a contributor's existing supported interpreter instead of replacing
# a Python 3.12 environment merely because .python-version recommends 3.13.
if [ -x "$ROOT/.venv/bin/python" ]; then
  (cd "$ROOT" && uv sync --locked --extra dev --python "$ROOT/.venv/bin/python")
else
  (cd "$ROOT" && uv sync --locked --extra dev)
fi
(cd "$ROOT" && uv run --no-sync python scripts/check-sherpa-source.py)

BINARIES="$TAURI_DIR/binaries"
mkdir -p "$BINARIES"
if [ "$HOST_ARCH" = arm64 ]; then
  WORKER="$WORKER_DIR/target/release/whisperstream-macos-stt-worker"
  WORKER_NEEDS_BUILD=0
  if [ ! -x "$WORKER" ]; then
    WORKER_NEEDS_BUILD=1
  elif find "$WORKER_DIR/src" "$WORKER_DIR/Cargo.toml" "$WORKER_DIR/Cargo.lock" -type f -newer "$WORKER" -print -quit | grep -q .; then
    WORKER_NEEDS_BUILD=1
  fi
  if [ "$WORKER_NEEDS_BUILD" -eq 1 ]; then
    echo "[mac-dev] building the native macOS STT worker"
    MACOSX_DEPLOYMENT_TARGET=14.0 cargo build --release --locked \
      --manifest-path "$WORKER_DIR/Cargo.toml"
  fi

  WORKER_SIDECAR="$BINARIES/whisperstream-macos-stt-worker-$TARGET_TRIPLE"
  if [ ! -f "$WORKER_SIDECAR" ] || ! cmp -s "$WORKER" "$WORKER_SIDECAR"; then
    cp "$WORKER" "$WORKER_SIDECAR"
    chmod u+x "$WORKER_SIDECAR"
  fi
else
  # Intel uses sherpa-onnx on CPU. The FluidAudio worker cannot run here.
  # Tauri still validates every externalBin, even for source development.
  cat > "$BINARIES/whisperstream-macos-stt-worker-$TARGET_TRIPLE" <<'STUB'
#!/bin/sh
echo "FluidAudio STT is unavailable on Intel; use the CPU speech backend." >&2
exit 1
STUB
  chmod u+x "$BINARIES/whisperstream-macos-stt-worker-$TARGET_TRIPLE"
fi

# Debug builds never spawn the packaged engine, but Tauri validates every
# externalBin path before compiling.
ENGINE_STUB="$BINARIES/whisperstream-engine-$TARGET_TRIPLE"
if [ ! -e "$ENGINE_STUB" ]; then
  : > "$ENGINE_STUB"
  chmod u+x "$ENGINE_STUB"
fi

LLAMA_SIDECAR="$BINARIES/llama-server-$TARGET_TRIPLE"
LLAMA_LIBRARY="$TAURI_DIR/resources/llama-cpp/libllama.dylib"
if [ "$HOST_ARCH" = arm64 ]; then
  if [ ! -x "$LLAMA_SIDECAR" ] || [ ! -f "$LLAMA_LIBRARY" ]; then
    echo "[mac-dev] staging the pinned Metal llama-server and dylibs"
    (cd "$ROOT" && uv run --no-sync python scripts/fetch_macos_llama_server.py)
  fi
else
  # Managed local AI cleanup requires the Metal runtime. Intel supports
  # dictation, Agent Speak and optional external cleanup providers instead.
  cat > "$LLAMA_SIDECAR" <<'STUB'
#!/bin/sh
echo "Managed local AI cleanup is unavailable in the Intel source build." >&2
exit 1
STUB
  chmod u+x "$LLAMA_SIDECAR"
  mkdir -p "$(dirname "$LLAMA_LIBRARY")"
  echo "Managed local AI cleanup is unavailable on Intel." > "$(dirname "$LLAMA_LIBRARY")/intel-unavailable.txt"
  echo "[mac-dev] Intel CPU speech enabled; managed local AI cleanup is unavailable"
fi

# This is the standalone terminal wrapper, not a packaged application. Tauri
# validates every staged externalBin above before compiling this crate.
(cd "$TAURI_DIR" && MACOSX_DEPLOYMENT_TARGET=14.0 cargo build --locked --bin whisperstream-cli)
mkdir -p "$TAURI_DIR/bin"
cp "$TAURI_DIR/target/debug/whisperstream-cli" "$TAURI_DIR/bin/whisperstream"

if [ "$BOOTSTRAP_ONLY" -eq 1 ]; then
  echo "[mac-dev] bootstrap complete"
  exit 0
fi

if command -v lsof >/dev/null 2>&1 && lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use. Stop the existing backend first." >&2
  exit 1
fi
if command -v lsof >/dev/null 2>&1 && lsof -tiTCP:1420 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port 1420 is already in use. Stop the existing Vite/Tauri session first." >&2
  exit 1
fi

BACKEND_ARGS=(uv run --no-sync python -m whisperstream.main --serve --port "$PORT")
if [ "$NO_HOTKEYS" -eq 1 ]; then
  BACKEND_ARGS+=(--no-hotkeys)
fi

BACKEND_PID=""
BACKEND_CREATED=""
PROCESS_OWNER="$ROOT/scripts/macos_dev_process.py"
PROCESS_PYTHON="$ROOT/.venv/bin/python"
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [ -n "$BACKEND_PID" ] && [ -n "$BACKEND_CREATED" ]; then
    "$PROCESS_PYTHON" "$PROCESS_OWNER" stop "$BACKEND_PID" --created "$BACKEND_CREATED" || true
    wait "$BACKEND_PID" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

echo "[mac-dev] starting backend with data dir: $WHISPERSTREAM_DATA_DIR"
(cd "$ROOT" && exec "${BACKEND_ARGS[@]}") &
BACKEND_PID=$!
BACKEND_CREATED="$("$PROCESS_PYTHON" "$PROCESS_OWNER" identity "$BACKEND_PID" --parent "$$")"

BACKEND_READY=0
for ((attempt = 0; attempt < 120; attempt++)); do
  if "$PROCESS_PYTHON" "$PROCESS_OWNER" ready "$BACKEND_PID" --created "$BACKEND_CREATED" --port "$PORT" && curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    BACKEND_READY=1
    break
  fi
  if ! kill -0 "$BACKEND_PID" >/dev/null 2>&1; then
    wait "$BACKEND_PID"
    exit 1
  fi
  sleep 0.25
done
if [ "$BACKEND_READY" -ne 1 ]; then
  echo "Backend did not become ready on port $PORT." >&2
  exit 1
fi

if [ "$NO_TAURI" -eq 1 ]; then
  echo "[mac-dev] starting Vite at http://localhost:1420"
  (cd "$FRONTEND" && npm run dev)
else
  echo "[mac-dev] starting Tauri with the repo-local pinned CLI"
  # Match the CLI build's deployment target so the shared target directory
  # keeps its native dependencies instead of rebuilding them every launch.
  (cd "$TAURI_DIR" && MACOSX_DEPLOYMENT_TARGET=14.0 "$FRONTEND/node_modules/.bin/tauri" dev)
fi
