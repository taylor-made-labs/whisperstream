import logging
import sys
import unittest
from unittest.mock import MagicMock, patch

from whisperstream.audio_feedback import AudioFeedback


def _fake_pygame_modules():
    """Build a MagicMock tree for pygame + pygame.mixer suitable for sys.modules."""
    fake_mixer = MagicMock()
    fake_pygame = MagicMock()
    fake_pygame.mixer = fake_mixer
    return {"pygame": fake_pygame, "pygame.mixer": fake_mixer}, fake_mixer


class TestAudioFeedbackCache(unittest.TestCase):
    def setUp(self):
        self.logger = MagicMock(spec=logging.Logger)

    def test_caching_pygame(self):
        """Second play_start reuses the cached pygame.mixer.Sound object."""
        fakes, fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes):
            af = AudioFeedback(logger=self.logger, enabled=True)

            # First call — constructs + plays.
            af.play_start()
            # Second call — should hit cache path; no new Sound constructed.
            af.play_start()

            # Sound constructor invoked exactly once across both play_start calls.
            self.assertEqual(fake_mixer.Sound.call_count, 1)

            # The cached Sound's .play() was invoked for both calls.
            returned_sound = fake_mixer.Sound.return_value
            self.assertEqual(returned_sound.play.call_count, 2)

    def test_play_error_uses_winsound_messagebeep(self):
        """play_error with no override path triggers winsound.MessageBeep."""
        fakes, _fake_mixer = _fake_pygame_modules()
        fake_winsound = MagicMock()
        fake_winsound.MB_ICONHAND = 16

        merged = {**fakes, "winsound": fake_winsound}
        with patch.dict(sys.modules, merged), patch("whisperstream.audio_feedback.sys.platform", "win32"):
            af = AudioFeedback(logger=self.logger, enabled=True)
            af.play_error()

            fake_winsound.MessageBeep.assert_called_once_with(16)

    def test_play_error_without_override_skips_unsupported_system_beep(self):
        """Mac/Linux error handling must not import the Windows-only module."""
        for host in ("darwin", "linux"):
            with self.subTest(platform=host):
                fakes, fake_mixer = _fake_pygame_modules()
                with patch.dict(sys.modules, fakes), patch("whisperstream.audio_feedback.sys.platform", host):
                    af = AudioFeedback(logger=self.logger, enabled=True)
                    with patch("builtins.__import__", wraps=__import__) as imports:
                        af.play_error()
                    self.assertNotIn("winsound", [call.args[0] for call in imports.call_args_list])
                    fake_mixer.Sound.assert_not_called()
                    self.logger.exception.assert_not_called()

    def test_play_error_custom_sound_keeps_mac_mixer_path(self):
        fakes, fake_mixer = _fake_pygame_modules()
        with patch.dict(sys.modules, fakes), patch("whisperstream.audio_feedback.sys.platform", "darwin"):
            af = AudioFeedback(logger=self.logger, enabled=True, volume=0.3)
            with patch.object(af, "_get_sound_path") as sound_path:
                sound_path.return_value.__enter__.return_value = "synthetic-error.wav"
                af.play_error("synthetic-error.wav")
            fake_mixer.Sound.assert_called_once_with("synthetic-error.wav")
            fake_mixer.Sound.return_value.set_volume.assert_called_once_with(0.3)
            fake_mixer.Sound.return_value.play.assert_called_once_with()

    def test_muted_error_never_plays_a_custom_sound_or_imports_winsound(self):
        af = AudioFeedback(logger=self.logger, enabled=False)
        with patch.object(af, "_play_sound") as play, patch("builtins.__import__", wraps=__import__) as imports:
            af.play_error()
            af.play_error("synthetic-error.wav")
        play.assert_not_called()
        self.assertNotIn("winsound", [call.args[0] for call in imports.call_args_list])


if __name__ == "__main__":
    unittest.main()
