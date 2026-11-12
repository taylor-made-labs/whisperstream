"""Opt-in CPU regression checks using generated buffers and cached models.

No application, input listener, microphone, or audio playback is started.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.skipif(
    not os.environ.get("WHISPERSTREAM_TEST_MODEL_BASE"),
    reason="set WHISPERSTREAM_TEST_MODEL_BASE to an isolated cached-model profile",
)
@pytest.mark.parametrize("model", ["nemo-parakeet-tdt-0.6b-v2", "nemo-parakeet-tdt-0.6b-v3"])
def test_native_cpu_handles_tiny_boundary_and_longer_generated_buffers(model: str) -> None:
    base = Path(os.environ["WHISPERSTREAM_TEST_MODEL_BASE"])
    model_dir = base / "models" / f"{model}-int8"
    if not all((model_dir / name).is_file() for name in (
        "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt",
    )):
        pytest.skip("isolated cached Parakeet model is incomplete")
    code = textwrap.dedent("""
        import json
        import sys
        import numpy as np
        from whisperstream.stt_worker_parakeet import ParakeetWorkerRuntime
        from whisperstream.stt_worker_protocol import ValidatedRequest, encode_audio_request

        runtime = ParakeetWorkerRuntime()
        load = {'model_name': sys.argv[1], 'model_dir': sys.argv[2],
                'quantization': 'int8', 'threads': 2}
        runtime.dispatch(ValidatedRequest('load', 'load', 'parakeet', load), b'')
        checked = 0
        for size in (0, 1, 56, 70, 79, 80, 81, 84, 160, 400, 1600, 16000):
            for kind in ('zero', 'tone'):
                audio = (np.zeros(size, dtype=np.float32) if kind == 'zero'
                         else (0.02 * np.sin(2 * np.pi * 440 * np.arange(size) / 16000)).astype(np.float32))
                encoded, payload = encode_audio_request(audio, sample_rate=16000)
                for operation in ('transcribe', 'transcribe_chunk', 'preview'):
                    request = ValidatedRequest(str(checked), operation, 'parakeet', encoded)
                    body, should_exit = runtime.dispatch(request, payload)
                    assert not should_exit and runtime.loaded
                    assert isinstance(body['text'], str)
                    if size < 80:
                        assert body['text'] == ''
                    checked += 1
        print(json.dumps({'checked': checked, 'model': sys.argv[1]}))
    """)
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment.update(
        PYTHONPATH=str(root / "src"), OMP_NUM_THREADS="2",
        OPENBLAS_NUM_THREADS="2", VECLIB_MAXIMUM_THREADS="2",
    )
    result = subprocess.run(
        [sys.executable, "-c", code, model, str(model_dir)],
        cwd=root, env=environment, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stderr
    assert '"checked": 72' in result.stdout
