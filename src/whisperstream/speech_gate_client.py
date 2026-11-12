"""Native-free admission client; detector failures never become ASR failures."""
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
import logging
import math
from time import monotonic
import numpy as np
from .speech_gate_settings import SpeechGateSettings
from .stt_supervisor import default_worker_command
from .stt_worker_process import SttWorkerProcess


STARTUP_TIMEOUT_SECONDS = 60.0


class SpeechGateClient:
    def __init__(self, settings: SpeechGateSettings, *, diagnostic_dir: Path,
                 logger: logging.Logger, timeout: float = 5.0,
                 startup_timeout: float = STARTUP_TIMEOUT_SECONDS, process_factory=None):
        if not math.isfinite(startup_timeout) or startup_timeout <= 0:
            raise ValueError('Speech detector startup timeout must be positive and finite')
        self.settings = settings
        self.failed = False
        self._process = None
        self._timeout = timeout
        self._startup_timeout = startup_timeout
        self._logger = logger
        self._factory = process_factory or (lambda: SttWorkerProcess(
            default_worker_command(), backend='speech_gate', diagnostic_dir=diagnostic_dir,
            cwd=Path(__file__).resolve().parents[2]))

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.is_running

    def close(self):
        process, self._process = self._process, None
        if process is not None:
            try:
                process.shutdown(timeout=1.0)
            except Exception as exc:
                self.failed = True
                self._logger.warning(
                    "speech_gate unavailable action=bypass error_type=%s", type(exc).__name__)
                process.terminate_owned_worker(expected=False)

    def has_speech(self, audio: np.ndarray, *, sample_rate=16000) -> bool:
        if not self.settings.enabled or self.failed or sample_rate != 16000:
            return True
        if not audio.size or not np.any(audio):
            return False
        # Even an entirely speech-positive window cannot meet the configured
        # duration below this size. Reject before native imports or ASR work.
        if audio.size < math.ceil(self.settings.min_speech_ms * sample_rate / 1000):
            return False
        try:
            if self._process is None:
                # Cold native imports and the CPU model belong to one bounded
                # readiness budget, not the much shorter warm admission wait.
                deadline = monotonic() + self._startup_timeout

                def remaining_startup_time():
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutError('Speech detector startup timed out')
                    return remaining

                self._process = self._factory()
                self._process.start(timeout=remaining_startup_time())
                self._process.request('load', body=asdict(self.settings),
                                      timeout=remaining_startup_time())
            body = self._process.request_audio('admit', audio, sample_rate=sample_rate,
                                               timeout=self._timeout)
            admitted = body.get('admitted')
            failed = body.get('detector_failed')
            if type(admitted) is not bool or type(failed) is not bool:
                raise ValueError('Invalid speech admission reply')
            if failed:
                raise RuntimeError('Speech detector unavailable')
            return admitted
        except Exception as exc:
            self.failed = True
            try:
                self.close()
            except Exception:
                pass
            self._logger.warning('speech_gate unavailable action=bypass error_type=%s',
                                 type(exc).__name__)
            return True
