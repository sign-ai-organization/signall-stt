import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Perfil de tiempo real. Cuando está activo, los cinco parámetros que dominan la
# latencia cambian de default a valores que priorizan "que el usuario vea la
# seña ya" sobre "que el transcriber no se equivoque". Cualquier variable de
# entorno sigue mandando sobre el perfil, así que un despliegue concreto puede
# pedir latencia y corrección a la vez. El detalle de cada valor medido está en
# `docs/performance.md`.
REALTIME = _env_bool("REALTIME", True)


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

    # Modelo aparte para los parciales. El parcial se tira a la basura en cuanto
    # llega el final, así que no necesita la precisión de `MODEL_SIZE`: un
    # modelo chico cuesta ~3x menos y eso es directamente menos espera para el
    # usuario. Vacio = usar el mismo modelo para todo (comportamiento previo).
    PARTIAL_MODEL_SIZE: str = os.getenv("PARTIAL_MODEL_SIZE", "small")
    PARTIAL_COMPUTE_TYPE: str = os.getenv("PARTIAL_COMPUTE_TYPE", "float16")

    VAD_AGGRESSIVENESS: int = _env_int("VAD_AGGRESSIVENESS", 2)
    # El hangover de silencio es el mayor termino de la latencia: son los ms que
    # el pipeline espera despues de que la persona termino de hablar. 600 ms es
    # el piso por debajo del cual una pausa natural entre palabras parte la
    # frase en dos, o sea el trade-off correcto para señas (que se pueden encadenar).
    SILENCE_TIMEOUT_MS: int = _env_int("SILENCE_TIMEOUT_MS", 600 if REALTIME else 1500)
    # El intervalo real de un parcial es este + la inferencia anterior, porque
    # solo hay una inferencia parcial en vuelo por sesion. Bajarlo de 400 a 250
    # compra un parcial mas rapido sin cambiar la concurrencia.
    PARTIAL_INTERVAL_MS: int = _env_int("PARTIAL_INTERVAL_MS", 250 if REALTIME else 400)  # 0 = desactivados
    MIN_AUDIO_MS_FOR_PARTIAL: int = _env_int("MIN_AUDIO_MS_FOR_PARTIAL", 160)
    MAX_BUFFER_MS: int = _env_int("MAX_BUFFER_MS", 8000)
    # Pausa mínima donde se corta una frase que llega a MAX_BUFFER_MS
    CUT_PAUSE_MIN_MS: int = _env_int("CUT_PAUSE_MIN_MS", 150)
    # Inicio de habla: ventana reciente y proporción mínima de frames con habla.
    # En tiempo real la ventana se acorta a 150 ms: son 150 ms que el usuario
    # espera sin que exista siquiera un buffer del cual sacar un parcial.
    SPEECH_ONSET_WINDOW_MS: int = _env_int("SPEECH_ONSET_WINDOW_MS", 150 if REALTIME else 300)
    SPEECH_ONSET_RATIO: float = _env_float("SPEECH_ONSET_RATIO", 0.7 if REALTIME else 0.9)
    # Silencio final que se conserva al transcribir el final
    FINAL_TRAILING_SILENCE_KEEP_MS: int = _env_int("FINAL_TRAILING_SILENCE_KEEP_MS", 120)

    BEAM_SIZE: int = _env_int("BEAM_SIZE", 1)
    INITIAL_PROMPT: str | None = os.getenv("INITIAL_PROMPT") or None

    MIN_RMS: float = _env_float("MIN_RMS", 0.008)
    # Percentil del volumen por tramo de 30 ms que se compara contra MIN_RMS
    MIN_RMS_PERCENTILE: float = _env_float("MIN_RMS_PERCENTILE", 90)
    NO_SPEECH_THRESHOLD: float = _env_float("NO_SPEECH_THRESHOLD", 0.6)
    # -1.0 es el default de faster-whisper y de whisper mismo. El valor anterior
    # (-0.8) descartaba frases enteras que el modelo si habia transcrito bien:
    # en los fixtures de `scripts/make_audio_fixtures.py` se comia "como estas"
    # y "estoy triste", que luego salen como transcripcion vacia. Descartar audio
    # real es peor que aceptar una frase dudosa, asi que se vuelve al default y
    # el filtro se limita a descartar silencio de verdad.
    AVG_LOGPROB_THRESHOLD: float = _env_float("AVG_LOGPROB_THRESHOLD", -1.0)

    # Timestamps por palabra. Cuestan un alineamiento con atencion cruzada por
    # segmento, pero son los que le dicen al backend que palabra ya se termino
    # de decir y cual es la que todavia se esta formando: sin eso, la unica
    # regla posible es "no firmes la ultima palabra", que es una aproximacion.
    # El detalle del costo medido esta en `docs/performance.md`.
    WORD_TIMESTAMPS: bool = _env_bool("WORD_TIMESTAMPS", True)

    # Cantidad maxima de inferencias simultaneas en la GPU. Whisper serializa
    # igual por debajo (CTranslate2 con num_workers=1), pero sin este limite
    # cada conexion lanza su propia inferencia y se acumulan en la cola de
    # CTranslate2, donde un parcial puede retrasar un final varios segundos.
    # 1 + el orden de prioridad de `InferenceScheduler` hace que eso no ocurra.
    MAX_CONCURRENT_INFERENCE: int = _env_int("MAX_CONCURRENT_INFERENCE", 1)

    @property
    def frame_size(self) -> int:
        return int(self.SAMPLE_RATE * self.FRAME_DURATION_MS / 1000)

    @property
    def bytes_per_frame(self) -> int:
        return self.frame_size * 2  # PCM16 = 2 bytes por muestra

    @property
    def partial_model_size(self) -> str:
        return self.PARTIAL_MODEL_SIZE or self.MODEL_SIZE

    @property
    def partial_compute_type(self) -> str:
        return self.PARTIAL_COMPUTE_TYPE if self.PARTIAL_MODEL_SIZE else self.COMPUTE_TYPE


settings = Settings()