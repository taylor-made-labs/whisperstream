from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "run_dev.sh"
FRONTEND = ROOT / "frontend"


def test_macos_launcher_uses_repo_local_pins() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/env bash\n")
    assert '"$FRONTEND/node_modules/.bin/tauri" dev' in source
    assert "npm ci --include=optional" in source
    assert '--os=darwin --cpu="$NODE_ARCH"' in source
    assert "package-lock.json" in source
    assert "scripts/fetch_macos_llama_server.py" in source
    assert "native/macos-stt-worker" in source
    assert "cargo install" not in source


def test_macos_launcher_uses_locked_official_sherpa_wheels() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    assert 'uv sync --locked --extra dev --python "$ROOT/.venv/bin/python"' in source
    assert "scripts/check-sherpa-source.py" in source
    assert "build_macos_sherpa" not in source
    assert "--inexact" not in source
    assert ".local-dependencies" not in source


def test_frontend_lock_pins_native_bindings_for_ci_and_supported_hosts() -> None:
    assert not (FRONTEND / ".npmrc").exists()
    lock = json.loads((FRONTEND / "package-lock.json").read_text(encoding="utf-8"))
    packages = lock["packages"]

    required = {
        "node_modules/@tauri-apps/cli-darwin-arm64",
        "node_modules/@tauri-apps/cli-darwin-x64",
        "node_modules/@tauri-apps/cli-linux-x64-gnu",
        "node_modules/@tauri-apps/cli-win32-x64-msvc",
        "node_modules/@rollup/rollup-darwin-arm64",
        "node_modules/@rollup/rollup-darwin-x64",
        "node_modules/@rollup/rollup-linux-x64-gnu",
        "node_modules/@rollup/rollup-win32-x64-msvc",
        "node_modules/@esbuild/darwin-arm64",
        "node_modules/@esbuild/darwin-x64",
        "node_modules/@esbuild/linux-x64",
        "node_modules/@esbuild/win32-x64",
        "node_modules/@tailwindcss/oxide-darwin-arm64",
        "node_modules/@tailwindcss/oxide-darwin-x64",
        "node_modules/@tailwindcss/oxide-linux-x64-gnu",
        "node_modules/@tailwindcss/oxide-win32-x64-msvc",
        "node_modules/lightningcss-darwin-arm64",
        "node_modules/lightningcss-darwin-x64",
        "node_modules/lightningcss-linux-x64-gnu",
        "node_modules/lightningcss-win32-x64-msvc",
    }
    assert required <= packages.keys()


def test_launcher_readiness_and_cleanup_are_bound_to_process_generation():
    source = LAUNCHER.read_text(encoding="utf-8")
    assert 'exec "${BACKEND_ARGS[@]}"' in source
    assert 'identity "$BACKEND_PID" --parent "$$"' in source
    assert 'ready "$BACKEND_PID" --created "$BACKEND_CREATED"' in source
    assert 'stop "$BACKEND_PID" --created "$BACKEND_CREATED"' in source
    assert '/api/shutdown' not in source


def _bootstrap_source_launcher(tmp_path, machine, node_arch=None, existing_environment=True):
    """Exercise architecture staging without installing, recording or launching."""
    import os
    import shutil
    import subprocess

    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(LAUNCHER, scripts / "run_dev.sh")
    frontend = checkout / "frontend/node_modules"
    for name in ("vite", "@tauri-apps/cli"):
        target = frontend / name / "package.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
    worker_dir = checkout / "native/macos-stt-worker"
    (worker_dir / "src").mkdir(parents=True)
    for name in ("Cargo.toml", "Cargo.lock"):
        (worker_dir / name).touch()
    worker = worker_dir / "target/release/whisperstream-macos-stt-worker"
    worker.parent.mkdir(parents=True)
    worker.write_text("native worker fixture")
    worker.chmod(0o755)
    binaries = checkout / "src-tauri/binaries"
    binaries.mkdir(parents=True)
    library = checkout / "src-tauri/resources/llama-cpp/libllama.dylib"
    library.parent.mkdir(parents=True)
    library.write_text("native library fixture")
    sidecar = binaries / "llama-server-aarch64-apple-darwin"
    sidecar.write_text("native llama fixture")
    sidecar.chmod(0o755)
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    tools = {
        "uname": '#!/bin/sh\nif [ "$1" = -s ]; then echo Darwin; else echo "$TEST_MACHINE"; fi\n',
        "node": '#!/bin/sh\nif [ "$1" = -p ]; then echo "$TEST_NODE_ARCH"; else cat >/dev/null; fi\n',
        "cargo": '#!/bin/sh\necho "cargo $*" >> "$TEST_LOG"\nmkdir -p "$TEST_ROOT/src-tauri/target/debug"\necho cli > "$TEST_ROOT/src-tauri/target/debug/whisperstream-cli"\n',
        "uv": '#!/bin/sh\necho "uv $*" >> "$TEST_LOG"\n',
        "npm": '#!/bin/sh\necho "npm $*" >> "$TEST_LOG"\n',
        "curl": '#!/bin/sh\nexit 99\n',
    }
    for name, text in tools.items():
        path = tools_dir / name
        path.write_text(text)
        path.chmod(0o755)
    if existing_environment:
        interpreter = checkout / ".venv/bin/python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("#!/bin/sh\nexit 0\n")
        interpreter.chmod(0o755)
    log = tmp_path / "commands.log"
    env = os.environ | {
        "PATH": str(tools_dir) + os.pathsep + os.environ["PATH"],
        "TEST_MACHINE": machine,
        "TEST_NODE_ARCH": node_arch or ("arm64" if machine == "arm64" else "x64"),
        "TEST_ROOT": str(checkout), "TEST_LOG": str(log),
        "WHISPERSTREAM_DATA_DIR": str(tmp_path / "isolated-state"),
    }
    result = subprocess.run(
        ["bash", str(scripts / "run_dev.sh"), "--bootstrap-only"],
        env=env, capture_output=True, text=True, timeout=15,
    )
    return result, checkout, log.read_text() if log.exists() else ""


def test_intel_bootstrap_stages_matching_sidecars_without_arm_dependencies(tmp_path):
    import subprocess

    result, checkout, commands = _bootstrap_source_launcher(tmp_path, "x86_64")
    assert result.returncode == 0, result.stderr
    assert "bootstrap complete" in result.stdout
    assert "fetch_macos_llama_server" not in commands
    assert "--manifest-path" not in commands
    assert "--bin whisperstream-cli" in commands
    assert f"uv sync --locked --extra dev --python {checkout}/.venv/bin/python" in commands
    binaries = checkout / "src-tauri/binaries"
    assert (binaries / "whisperstream-engine-x86_64-apple-darwin").exists()
    for name in ("whisperstream-macos-stt-worker", "llama-server"):
        placeholder = binaries / f"{name}-x86_64-apple-darwin"
        failed = subprocess.run([str(placeholder)], capture_output=True, text=True)
        assert failed.returncode == 1
        assert "Intel" in failed.stderr
    assert (checkout / "src-tauri/bin/whisperstream").read_text().strip() == "cli"


def test_apple_silicon_bootstrap_keeps_native_worker(tmp_path):
    result, checkout, commands = _bootstrap_source_launcher(tmp_path, "arm64")
    assert result.returncode == 0, result.stderr
    native = checkout / "src-tauri/binaries/whisperstream-macos-stt-worker-aarch64-apple-darwin"
    assert native.read_text() == "native worker fixture"
    assert "Intel CPU speech" not in result.stdout
    assert not (checkout / "src-tauri/binaries/llama-server-x86_64-apple-darwin").exists()


def test_launcher_rejects_mismatched_node_before_staging(tmp_path):
    result, checkout, commands = _bootstrap_source_launcher(tmp_path, "x86_64", "arm64")
    assert result.returncode == 1
    assert "Node must run natively" in result.stderr
    assert not commands
    assert not (checkout / "src-tauri/binaries/whisperstream-engine-x86_64-apple-darwin").exists()


def test_clean_checkout_bootstrap_uses_default_python_without_empty_bash_array(tmp_path):
    result, _checkout, commands = _bootstrap_source_launcher(
        tmp_path, "x86_64", existing_environment=False,
    )
    assert result.returncode == 0, result.stderr
    assert "uv sync --locked --extra dev\n" in commands
    assert "--python" not in commands
