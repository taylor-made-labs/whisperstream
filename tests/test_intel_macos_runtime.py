"""Intel uses Mac OS adapters and portable CPU speech, never FluidAudio."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from whisperstream.agent_integration import create_agent_integration_adapters
from whisperstream.agent_integration_contract import AgentHookAdapter, AgentPluginAdapter
from whisperstream.advanced_capabilities import create_advanced_capability_report
from whisperstream.daily_use_capabilities import create_daily_use_capability_report
from whisperstream.model_lifecycle import PlatformModelLifecycleFactory
from whisperstream.macos_onnx_model_lifecycle import MacosOnnxModelLifecycleAdapter
from whisperstream.stt_factory import make_stt_manager, parakeet_model_present, uses_fluidaudio_stt


@pytest.mark.parametrize("machine", ["arm64", "aarch64", "x86_64"])
def test_macos_agent_integration_supported_on_both_architectures(machine):
    plugin, hook = create_agent_integration_adapters(system="Darwin", machine=machine)
    assert type(plugin).__name__ == "MacOSAgentPluginAdapter"
    assert type(hook).__name__ == "MacOSAgentHookAdapter"
    assert isinstance(plugin, AgentPluginAdapter)
    assert isinstance(hook, AgentHookAdapter)


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [("darwin", "arm64", True), ("darwin", "aarch64", True),
     ("darwin", "x86_64", False), ("win32", "AMD64", False),
     ("linux", "aarch64", False)],
)
def test_fluidaudio_is_only_selected_for_apple_silicon(system, machine, expected):
    with patch("whisperstream.stt_factory.sys.platform", system), patch(
        "whisperstream.stt_factory.platform.machine", return_value=machine
    ):
        assert uses_fluidaudio_stt() is expected


def test_intel_parakeet_uses_same_onnx_inventory_for_presence_and_construction():
    config = SimpleNamespace(
        language="en", stt_backend="parakeet", parakeet_model="nemo-parakeet-tdt-0.6b-v3",
        parakeet_quantization="int8", threads=3, model_timeout=120,
        word_boosts=[], hotword_score=1.5, beam_width=4,
    )
    with patch("whisperstream.stt_factory.sys.platform", "darwin"), patch(
        "whisperstream.stt_factory.platform.machine", return_value="x86_64"
    ), patch("whisperstream.stt_factory.is_present", return_value=True) as present, patch(
        "whisperstream.stt_factory.model_dir", return_value=Path("/models/parakeet")
    ), patch("whisperstream.stt_factory._macos_manager_class") as fluid, patch(
        "whisperstream.stt_factory.ParakeetManager"
    ) as cpu:
        assert parakeet_model_present(config, Mock())
        manager = make_stt_manager(config, logger=Mock(), config_manager=Mock())
        assert manager is cpu.return_value
        fluid.assert_not_called()
        assert present.call_args_list[0] == present.call_args_list[1]
        assert cpu.call_args.kwargs["model_dir"] == Path("/models/parakeet")


def test_intel_model_lifecycle_does_not_claim_coreml_assets():
    owner = PlatformModelLifecycleFactory(system="Darwin", machine="x86_64").create()
    assert isinstance(owner, MacosOnnxModelLifecycleAdapter)
    # The server selects its Core ML download/delete paths only for owners
    # advertising this property; Intel must keep using registry ONNX files.
    assert not hasattr(owner, "stt_asset_id")


def test_intel_capabilities_do_not_advertise_apple_silicon_enhancement():
    report = create_daily_use_capability_report("Darwin", machine="x86_64").as_dict()
    capabilities = {item["id"]: item for item in report["capabilities"]}
    assert capabilities["model-lifecycle"]["reason"] == "onnx-verified"
    assert capabilities["managed-local-enhancement"]["state"] == "unavailable"
    advanced = create_advanced_capability_report("Darwin", machine="x86_64").as_dict()
    capabilities = {item["id"]: item for item in advanced["capabilities"]}
    assert capabilities["local-llm-runtime"]["state"] == "unavailable"


def test_intel_speech_uses_macos_output_adapters():
    from whisperstream.tts_playback import (
        create_speech_playback_adapter, create_speech_synthesis_adapter,
    )
    synthesis = create_speech_synthesis_adapter(
        engine_manager=Mock(), system="Darwin", machine="x86_64",
    )
    playback = create_speech_playback_adapter(
        system="Darwin", machine="x86_64", sounddevice_module=Mock(),
    )
    assert type(synthesis).__name__ == "MacOSSpeechSynthesisAdapter"
    assert type(playback).__name__ == "MacOSSpeechPlaybackAdapter"
    synthesis.close()
    playback.close()


def test_intel_never_discovers_stale_arm64_cleanup_sidecar(tmp_path):
    from whisperstream import llama_server
    source = tmp_path / "src/whisperstream/llama_server.py"
    source.parent.mkdir(parents=True)
    source.write_text("# synthetic source")
    sidecar = tmp_path / "src-tauri/binaries/llama-server-aarch64-apple-darwin"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_bytes(b"synthetic arm64 executable")
    with patch.object(llama_server.platform, "system", return_value="Darwin"), patch.object(
        llama_server.platform, "machine", return_value="x86_64"
    ), patch.object(llama_server, "__file__", str(source)):
        assert llama_server.find_bundled_llama_server() is None


def test_intel_server_does_not_prepare_unsupported_cleanup_runtime():
    from whisperstream.server import WhisperstreamServer
    server = object.__new__(WhisperstreamServer)
    server._logger = Mock()
    # A stale configuration or even a finder override must not cause a child
    # supervisor to be created on Intel. No app/device construction is needed.
    with patch("whisperstream.llama_server.platform.system", return_value="Darwin"), patch(
        "whisperstream.llama_server.platform.machine", return_value="x86_64"
    ), patch("whisperstream.llama_server.find_bundled_llama_server") as find, patch(
        "whisperstream.llama_server.LlamaServerSupervisor"
    ) as supervisor:
        server._maybe_prepare_llama_server()
        find.assert_not_called()
        supervisor.assert_not_called()
