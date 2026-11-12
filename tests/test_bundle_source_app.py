"""Source bundling orchestration uses fake builders and never launches an app."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bundle_source_app", ROOT / "scripts/bundle_source_app.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def _checkout(tmp_path):
    root = tmp_path / "checkout"
    for name in ("pyproject.toml", "src/whisperstream/main.py", "scripts/run_dev.sh"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source fixture")
    return root


class FakeBuilders:
    def __init__(self, root, *, running=None, create_cli=True):
        self.root = root
        self.calls = []
        self.running = iter(running or [False, False])
        self.create_cli = create_cli

    def __call__(self, args, **kwargs):
        args = [str(arg) for arg in args]
        self.calls.append((args, kwargs))
        if args[0] == "/usr/sbin/lsof":
            active = next(self.running)
            return subprocess.CompletedProcess(args, 0 if active else 1, "123\n" if active else "", "")
        if args[0] == "/bin/bash":
            assert "--bootstrap-only" in args
            for name in (".venv/bin/python", "frontend/node_modules/.bin/tauri"):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("tool fixture")
            if self.create_cli:
                cli = self.root / "src-tauri/target/debug/whisperstream-cli"
                cli.parent.mkdir(parents=True, exist_ok=True)
                cli.write_text("CLI fixture")
        if args[1:2] == ["bundle"]:
            executable = self.root / "src-tauri/target/debug/bundle/macos/Whisperstream.app/Contents/MacOS/whisperstream"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text("real GUI fixture")
        if args[:4] == ["/usr/bin/codesign", "--force", "--sign", "-"] and args[-1].endswith(".app"):
            resource = Path(args[-1]) / "Contents/Resources/source-backend.json"
            assert resource.is_file(), "Configuration must be sealed by the final signature"
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.mark.parametrize("no_hotkeys", [False, True])
def test_builds_signed_source_app_without_launching(tmp_path, no_hotkeys):
    root = _checkout(tmp_path)
    data = tmp_path / "test-data"
    fake = FakeBuilders(root)
    original_env = {
        "PATH": "/synthetic/bin", "TAURI_SIGNING_PRIVATE_KEY": "synthetic",
        "WHISPERSTREAM_UPDATER_RS": "synthetic", "CARGO_BUILD_TARGET": "arm64-synthetic",
    }
    bundle = builder.build_bundle(root, data, no_hotkeys=no_hotkeys, run=fake,
                                  environment=original_env, system="Darwin", machine="x86_64")
    config = json.loads((bundle / "Contents/Resources/source-backend.json").read_text())
    assert config == {"source_root": str(root.resolve()), "data_dir": str(data.resolve()),
                      "no_hotkeys": no_hotkeys}
    assert (bundle / "Contents/MacOS/whisperstream").read_text() == "real GUI fixture"
    commands = [args for args, _ in fake.calls]
    assert commands[0][:2] == ["/bin/bash", str(root / "scripts/run_dev.sh")]
    assert ("--no-hotkeys" in commands[0]) is no_hotkeys
    assert commands[1][1:] == ["build", "--debug", "--no-bundle", "--ci"]
    assert commands[2] == ["/usr/bin/codesign", "--force", "--sign", "-",
                           str(root / "src-tauri/target/debug/whisperstream-cli")]
    assert commands[3][1:] == ["bundle", "--debug", "--bundles", "app", "--ci"]
    assert commands[-1] == ["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", str(bundle)]
    assert {Path(args[0]).name for args in commands} == {"bash", "tauri", "codesign"}
    for _args, kwargs in fake.calls:
        if "env" not in kwargs:
            continue
        env = kwargs["env"]
        assert "TAURI_SIGNING_PRIVATE_KEY" not in env
        assert "WHISPERSTREAM_UPDATER_RS" not in env
        assert "CARGO_BUILD_TARGET" not in env
        assert env["CARGO_TARGET_DIR"] == str(root / "src-tauri/target")
        assert env["WHISPERSTREAM_DATA_DIR"] == str(data)
    assert original_env["TAURI_SIGNING_PRIVATE_KEY"] == "synthetic"


@pytest.mark.parametrize("start_during_build", [False, True])
def test_refuses_to_replace_a_running_bundle(tmp_path, start_during_build):
    root = _checkout(tmp_path)
    executable = root / "src-tauri/target/debug/bundle/macos/Whisperstream.app/Contents/MacOS/whisperstream"
    executable.parent.mkdir(parents=True)
    executable.write_text("old GUI fixture")
    fake = FakeBuilders(root, running=[False, True] if start_during_build else [True])
    with pytest.raises(ValueError, match="Close the running source app"):
        builder.build_bundle(root, tmp_path / "data", run=fake, system="Darwin", machine="x86_64")
    assert not any(args[1:2] == ["bundle"] for args, _ in fake.calls)
    assert executable.read_text() == "old GUI fixture"
    if not start_during_build:
        assert len(fake.calls) == 1


def test_missing_cli_stops_before_bundling(tmp_path):
    root = _checkout(tmp_path)
    fake = FakeBuilders(root, create_cli=False)
    with pytest.raises(ValueError, match="command-line executable is missing"):
        builder.build_bundle(root, tmp_path / "data", run=fake, system="Darwin", machine="x86_64")
    assert not any(args[0] == "/usr/bin/codesign" or args[1:2] == ["bundle"] for args, _ in fake.calls)


@pytest.mark.parametrize("suffix", ["Whisperstream", "Whisperstream/child", "whisperstream"])
def test_refuses_installed_profile_and_case_variants(tmp_path, suffix):
    home = tmp_path / "home"
    with pytest.raises(ValueError, match="separate development data"):
        builder.source_data_dir(home / "Library/Application Support" / suffix, home=home)


def test_refuses_symlink_into_installed_profile(tmp_path):
    home = tmp_path / "home"
    installed = home / "Library/Application Support/Whisperstream"
    installed.mkdir(parents=True)
    link = tmp_path / "alias"
    link.symlink_to(installed, target_is_directory=True)
    with pytest.raises(ValueError, match="separate development data"):
        builder.source_data_dir(link / "child", home=home)


@pytest.mark.parametrize(("system", "machine"), [("Linux", "x86_64"), ("Darwin", "i386")])
def test_rejects_unsupported_host_before_build(tmp_path, system, machine):
    fake = FakeBuilders(tmp_path)
    with pytest.raises(ValueError, match="native Apple Silicon or Intel macOS"):
        builder.build_bundle(tmp_path, tmp_path / "data", run=fake, system=system, machine=machine)
    assert not fake.calls


def test_build_failure_is_not_hidden(tmp_path):
    root = _checkout(tmp_path)
    def fail(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)
    with pytest.raises(subprocess.CalledProcessError):
        builder.build_bundle(root, tmp_path / "data", run=fail, system="Darwin", machine="x86_64")
