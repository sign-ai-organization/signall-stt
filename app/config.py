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


# El servicio no tiene un "perfil de tiempo real". El 14 de octubre se agrandó
# uno que cambiaba cinco defaults según una bandera, y la bandera no era una
# reversión: `docker-compose.yml` fijaba los valores del perfil uno por uno, así
# que poner `REALTIME=false` solo cambiaba una cosa de las seis. La latencia se
# ajusta overrindiendo cada variable, que es lo que hace el compose.


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
    # 1500 ms es el valor del diagrama 6.3 del SDD y el que tiene el compose. Es
    # el mayor término de la latencia —son los ms que se espera después de que la
    # persona terminó de hablar—, pero bajarlo a 600 parte las frases en los
    # puntos donde la persona respira, y una frase partida son dos señas.
    SILENCE_TIMEOUT_MS: int = _env_int("SILENCE_TIMEOUT_MS", 1500)
    # Los parciales se quedan (RF-STT-02, del 30 de septiembre: anteriores al
    # perfil de tiempo real). El texto se muestra progresivo porque es un
    # requisito, no porque sirva para representar antes.
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
    # -1.0 es el default de faster-whisper y de whisper mismo. El valor anterior
    # (-0.8) descartaba frases enteras que el modelo sí había transcrito bien: en
    # los fixtures de `scripts/make_audio_fixtures.py` se comía "cómo estás" y
    # "estoy triste", que luego salen como transcripción vacía. Descartar audio
    # real es peor que aceptar una frase dudosa, así que se vuelve al default y
    # el filtro se limita a descartar silencio de verdad.
    AVG_LOGPROB_THRESHOLD: float = _env_float("AVG_LOGPROB_THRESHOLD", -1.0)

    # Timestamps por palabra. Cuestan un alineamiento con atención cruzada por
    # segmento y salen en el payload, así que están apagados por defecto.
    #
    # Nadie los lee: existían para que el backend decidiera qué parte de la frase
    # ya se había terminado de decir, que era la regla del camino por fragmentos.
    # Con la unidad de traducción siendo la oración, la frase se representa cuando
    # termina, no a mitad de camino, y el backend ya no mira el campo.
    #
    # Se deja el ajuste y no se borra el código porque es la forma de volver a
    # pedir los tiempos sin tocar nada, y porque `scripts/make_audio_fixtures.py`
    # y el endpoint `/transcribe` los muestran para diagnosticar una transcripción
    # rara. Si algún día no los quiere nadie, se borran acá y en
    # `TranscriptionResult.to_payload`: el backend los parsea de forma tolerante.
    WORD_TIMESTAMPS: bool = _env_bool("WORD_TIMESTAMPS", False)

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


settings = Settings()