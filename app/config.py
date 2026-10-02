import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = _env_int("PORT", 8000)

    SAMPLE_RATE: int = _env_int("SAMPLE_RATE", 16000)
    FRAME_DURATION_MS: int = _env_int("FRAME_DURATION_MS", 30)  # WebRTC VAD solo acepta 10, 20 o 30

    MODEL_SIZE: str = os.getenv("MODEL_SIZE", "medium")
    DEVICE: str = os.getenv("DEVICE", "cuda")
    COMPUTE_TYPE: str = os.getenv("COMPUTE_TYPE", "float16")
    LANGUAGE: str = os.getenv("LANGUAGE", "es")

    VAD_AGGRESSIVENESS: int = _env_int("VAD_AGGRESSIVENESS", 2)
    SILENCE_TIMEOUT_MS: int = _env_int("SILENCE_TIMEOUT_MS", 1500)
    PARTIAL_INTERVAL_MS: int = _env_int("PARTIAL_INTERVAL_MS", 400)  # 0 = parciales desactivados
    MIN_AUDIO_MS_FOR_PARTIAL: int = _env_int("MIN_AUDIO_MS_FOR_PARTIAL", 200)
    MAX_BUFFER_MS: int = _env_int("MAX_BUFFER_MS", 8000)
    # Pausa mínima donde se corta una frase que llega a MAX_BUFFER_MS
    CUT_PAUSE_MIN_MS: int = _env_int("CUT_PAUSE_MIN_MS", 150)
    # Inicio de habla: ventana reciente y proporción mínima de frames con habla
    SPEECH_ONSET_WINDOW_MS: int = _env_int("SPEECH_ONSET_WINDOW_MS", 300)
    SPEECH_ONSET_RATIO: float = _env_float("SPEECH_ONSET_RATIO", 0.9)
    # Silencio final que se conserva al transcribir el final
    FINAL_TRAILING_SILENCE_KEEP_MS: int = _env_int("FINAL_TRAILING_SILENCE_KEEP_MS", 300)

    BEAM_SIZE: int = _env_int("BEAM_SIZE", 1)
    INITIAL_PROMPT: str | None = os.getenv("INITIAL_PROMPT") or None

    MIN_RMS: float = _env_float("MIN_RMS", 0.008)
    # Percentil del volumen por tramo de 30 ms que se compara contra MIN_RMS
    MIN_RMS_PERCENTILE: float = _env_float("MIN_RMS_PERCENTILE", 90)
    NO_SPEECH_THRESHOLD: float = _env_float("NO_SPEECH_THRESHOLD", 0.6)
    AVG_LOGPROB_THRESHOLD: float = _env_float("AVG_LOGPROB_THRESHOLD", -0.8)

    @property
    def frame_size(self) -> int:
        return int(self.SAMPLE_RATE * self.FRAME_DURATION_MS / 1000)

    @property
    def bytes_per_frame(self) -> int:
        return self.frame_size * 2  # PCM16 = 2 bytes por muestra


settings = Settings()
