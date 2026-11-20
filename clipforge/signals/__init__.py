"""Signal plugins. Each is one file; add new ones to DEFAULT_SIGNALS."""

from __future__ import annotations

from clipforge.ports.analysis import SignalExtractor
from clipforge.signals.audio_energy import AudioEnergy
from clipforge.signals.chat_velocity import ChatVelocity
from clipforge.signals.emote_burst import EmoteBurst
from clipforge.signals.speech_intensity import SpeechIntensity


def default_signals() -> list[SignalExtractor]:
    return [ChatVelocity(), EmoteBurst(), AudioEnergy(), SpeechIntensity()]


__all__ = ["AudioEnergy", "ChatVelocity", "EmoteBurst", "SpeechIntensity", "default_signals"]
