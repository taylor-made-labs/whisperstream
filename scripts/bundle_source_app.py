#!/usr/bin/env python3
"""Build a local macOS source app without launching it or changing OS trust.

The signed GUI owns the source Python child, keeping permission requests under
the app's process responsibility instead of a separately launched interpreter.
The bundle remains tied to this checkout and its virtual environment.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
_PRIVATE_BUILD_ENV = (
    "TAURI_SIGNING_PRIVATE_KEY", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD",
    "WHISPERSTREAM_UPDATER_RS", "WHISPERSTREAM_FRONTEND_EDITION",
    "WHISPERSTREAM_UPDATER_POLICY", "WHISPERSTREAM_TAURI_OVERLAY",
    "WHISPERSTREAM_BUILD_SIGNING_HOME",
)
Run = Callable[..., subprocess.CompletedProcess]


def source_data_dir(value: Path, *, home: Path | None = None) -> Path:
    """Resolve symlinks and refuse the installed application's profile."""
    candidate = value.expanduser().resolve()
    installed = ((home or Path.home()) / "Library/Application Support/Whisperstream").resolve()
    # Refuse case variants too, including on case-sensitive development disks.
    wanted = tuple(part.casefold() for part in candidate.parts)
    protected = tuple(part.casefold() for part in installed.parts)
    if wanted[:len(protected)] == protected:
        raise ValueError("Use a separate development data directory, not the installed app profile.")
    if candidate.exists() and not candidate.is_dir():
        raise ValueError("The development data directory names an existing file.")
    return candidate


def require_native_macos(system: str, machine: str) -> None:
    if system != "Darwin" or machine.lower() not in {"arm64", "aarch64", "x86_64"}:
        raise ValueError("Source app bundles require native Apple Silicon or Intel macOS.")


def assert_bundle_idle(bundle: Path, run: Run) -> None:
    """Never overwrite the executable of an app that is still running."""
    executable = bundle / "Contents/MacOS/whisperstream"
    if not executable.exists():
        return
    result = run(
        ["/usr/sbin/lsof", "-t", str(executable)],
        check=False, capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        raise ValueError("Close the running source app before rebuilding its bundle.")
    if result.returncode != 1:
        raise ValueError("Could not confirm that the source app is closed; build stopped.")


def build_bundle(
    source_root: Path,
    data_dir: Path,
    *,
    no_hotkeys: bool = False,
    run: Run = subprocess.run,
    environment: dict[str, str] | None = None,
    system: str | None = None,
    machine: str | None = None,
    home: Path | None = None,
) -> Path:
    require_native_macos(system or platform.system(), machine or platform.machine())
    source_root = source_root.expanduser().resolve()
    for name in ("pyproject.toml", "src/whisperstream/main.py", "scripts/run_dev.sh"):
        if not (source_root / name).is_file():
            raise ValueError("The source checkout is incomplete.")
    data_dir = source_data_dir(data_dir, home=home)
    tauri_dir = source_root / "src-tauri"
    bundle = tauri_dir / "target/debug/bundle/macos/Whisperstream.app"
    assert_bundle_idle(bundle, run)

    env = dict(os.environ if environment is None else environment)
    for name in (*_PRIVATE_BUILD_ENV, "CARGO_BUILD_TARGET"):
        env.pop(name, None)
    env.update(
        WHISPERSTREAM_DATA_DIR=str(data_dir),
        WHISPERSTREAM_AGENT_PLUGIN_VARIANT="dev",
        CARGO_TARGET_DIR=str(tauri_dir / "target"),
        MACOSX_DEPLOYMENT_TARGET="14.0",
    )
    bootstrap = ["/bin/bash", str(source_root / "scripts/run_dev.sh"),
                 "--bootstrap-only", "--data-dir", str(data_dir)]
    if no_hotkeys:
        bootstrap.append("--no-hotkeys")
    run(bootstrap, cwd=source_root, env=env, check=True)
    python = source_root / ".venv/bin/python"
    tauri = source_root / "frontend/node_modules/.bin/tauri"
    if not python.is_file() or not tauri.is_file():
        raise ValueError("Source bootstrap did not prepare Python and the pinned Tauri CLI.")

    # Tauri build runs the normal production frontend build, but bundling is
    # separate so the additional managed CLI is signed before it is copied.
    run([str(tauri), "build", "--debug", "--no-bundle", "--ci"],
        cwd=tauri_dir, env=env, check=True)
    cli = tauri_dir / "target/debug/whisperstream-cli"
    if not cli.is_file():
        raise ValueError("The managed command-line executable is missing after bootstrap.")
    run(["/usr/bin/codesign", "--force", "--sign", "-", str(cli)],
        cwd=source_root, env=env, check=True)
    # Recheck after compilation: a contributor could have opened the old app
    # while the build was running. Never replace that live executable.
    assert_bundle_idle(bundle, run)
    run([str(tauri), "bundle", "--debug", "--bundles", "app", "--ci"],
        cwd=tauri_dir, env=env, check=True)

    if not (bundle / "Contents/MacOS/whisperstream").is_file():
        raise ValueError("Tauri did not produce the source app bundle.")
    resource_dir = bundle / "Contents/Resources"
    resource_dir.mkdir(parents=True, exist_ok=True)
    config = resource_dir / "source-backend.json"
    if config.is_symlink():
        raise ValueError("The source backend configuration must not be a symlink.")
    config.write_text(json.dumps({
        "source_root": str(source_root),
        "data_dir": str(data_dir),
        "no_hotkeys": bool(no_hotkeys),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    run(["/usr/bin/codesign", "--force", "--sign", "-", str(bundle)],
        cwd=source_root, env=env, check=True)
    run(["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", str(bundle)],
        cwd=source_root, env=env, check=True)
    return bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path,
                        default=Path.home() / "Library/Application Support/Whisperstream-dev")
    parser.add_argument("--no-hotkeys", action="store_true",
                        help="Keep global shortcuts disabled when this source app is launched.")
    args = parser.parse_args(argv)
    try:
        bundle = build_bundle(ROOT, args.data_dir, no_hotkeys=args.no_hotkeys)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Source app build failed: {exc}\n")
    print(f"Built source app (not launched): {bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
