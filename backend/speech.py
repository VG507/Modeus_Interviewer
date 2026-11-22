"""Speech to text for voice answers in the Telegram bot, with faster-whisper when it is installed.

Both the package and its model are optional: until `pip install faster-whisper` is done and
SPEECH_MODEL is set in .env, the bot asks for a text answer instead. The model loads on the
first voice message and stays in memory.
"""

import io
from functools import lru_cache
from importlib.util import find_spec

# Telegram lets a bot download files up to 20 MB; longer recordings are better split anyway.
MAX_VOICE_SECONDS = 300
MAX_VOICE_BYTES = 20 * 1024 * 1024


def configured(settings):
    return bool(settings.speech_model) and find_spec("faster_whisper") is not None


@lru_cache(maxsize=2)
def load_model(name):
    from faster_whisper import WhisperModel

    # int8 keeps a CPU fast and memory low; a GPU, when present, is picked by "auto".
    return WhisperModel(name, device="auto", compute_type="int8")


def transcribe(settings, audio):
    """Text of a recording (Telegram's OGG/Opus decodes without a system ffmpeg), "" when nothing was heard."""
    segments, _ = load_model(settings.speech_model).transcribe(
        io.BytesIO(audio), language=settings.speech_language, vad_filter=True, beam_size=5)
    return " ".join(segment.text.strip() for segment in segments).strip()
