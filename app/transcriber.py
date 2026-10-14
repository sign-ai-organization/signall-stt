"""Carga del modelo Whisper y transcripción de buffers de audio.

Dos cosas perecen a la latencia viven acá y en ningún otro lado:

1. **Quién tiene la GPU.** `InferenceScheduler` es el unico punto por el que
   pasa toda inferencia. Whisper internamente serializa igual (CTranslate2 con
   `num_workers=1`), pero sin un orden explicito el orden de llegada es el
   orden de ejecucion: un parcial que empezo antes que un final se ejecuta
   antes que el, y lo demostro `docs/performance.md` con picos de 4 a 8
   segundos. Con el scheduler, un final salta la cola de parciales.

2. **Que se descarta y por que.** `transcribe_buffer` devuelve un
   `TranscriptionResult` con el motivo del descarte y los timestamps por
   palabra, no solo el string. Un `""` sin explicación es indistinguible de un
   silencio real, y esa indistinción es la razon por la que dos frases de los
   fixtures se perdian sin dejar rastro.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import numpy as np
from faster_whisper import WhisperModel

from app.config import Settings
from app.metrics import ConcurrencyGauge, metrics, metrics_timer

T = TypeVar("T")

# Prioridades del scheduler. Menor numero = pasa primero.
PRIORITY_FINAL = 0
PRIORITY_PARTIAL = 10


@dataclass(frozen=True)
class WordTiming:
    """Una palabra y cuando se oyó, en ms relativos al inicio del buffer."""

    word: str
    start_ms: int
    end_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {"w": self.word, "s": self.start_ms, "e": self.end_ms}


@dataclass
class TranscriptionResult:
    text: str
    words: list[WordTiming] = field(default_factory=list)
    audio_ms: float = 0.0
    elapsed_ms: float = 0.0
    drop_reason: str | None = None
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    model: str = ""

    @property
    def dropped(self) -> bool:
        return self.drop_reason is not None

    def to_payload(self, kind: str) -> dict[str, Any]:
        """El frame JSON que va por el WebSocket.

        Los campos nuevos (`words`, `audio_ms`) son aditivos: el backend los
        ignora si no los entiende, y el `text` sigue siendo el campo que el
        cliente de referencia ya leia.
        """
        payload: dict[str, Any] = {"type": kind, "text": self.text}
        if self.words:
            payload["words"] = [word.to_dict() for word in self.words]
        payload["audio_ms"] = round(self.audio_ms)
        return payload


def load_model(settings: Settings, model_size: str | None = None, compute_type: str | None = None) -> WhisperModel:
    size = model_size or settings.MODEL_SIZE
    compute = compute_type or settings.COMPUTE_TYPE
    print(f"Cargando modelo {size} ({settings.DEVICE}, {compute})...", flush=True)
    try:
        model = WhisperModel(size, device=settings.DEVICE, compute_type=compute)
        print(f"Modelo cargado en {settings.DEVICE}.", flush=True)
        return model
    except Exception as exc:
        if settings.DEVICE == "cuda":
            print(f"[ADVERTENCIA] GPU no disponible: {exc}\nCayendo a CPU...", flush=True)
            model = WhisperModel(size, device="cpu", compute_type="int8")
            print("Modelo cargado en CPU.", flush=True)
            return model
        raise


def load_models(settings: Settings) -> dict[str, WhisperModel]:
    """Carga el modelo de finales y, si difiere, el de parciales.

    El de parciales es opcional a proposito: en una maquina con poca VRAM cabe
    uno solo, y en ese caso los parciales usan `MODEL_SIZE`, que es
    exactamente el comportamiento anterior a tiempo real.
    """
    models = {"final": load_model(settings)}
    if settings.partial_model_size and settings.partial_model_size != settings.MODEL_SIZE:
        models["partial"] = load_model(settings, settings.partial_model_size, settings.partial_compute_type)
    for key, model in models.items():
        warm_up(model, settings, label=key)
    return models


def warm_up(model: WhisperModel, settings: Settings, label: str = "final") -> None:
    """Primera inferencia deDummy, para que la real no pague la compilación de CUDA.

    La primera llamada a `generate()` de CTranslate2 reserva memoria, compila
    kernels y llena caches; medir eso dentro de la primer frase del usuario
    cuesta entre 500 y 800 ms de golpe. Descargarlo en el arranque lo saca de
    la ruta caliente.
    """
    started = time.monotonic()
    silence = np.zeros(settings.SAMPLE_RATE, dtype=np.float32)
    segments, _ = model.transcribe(silence, language=settings.LANGUAGE, beam_size=settings.BEAM_SIZE)
    list(segments)  # transcribe() es perezoso
    elapsed_ms = (time.monotonic() - started) * 1000
    print(f"Modelo '{label}' precalentado en {elapsed_ms:.0f} ms.", flush=True)
    metrics.histogram("stt_warmup_ms", elapsed_ms, "Costo del precalentado por modelo", {"model": label})


class InferenceScheduler:
    """Unico acceso a la GPU, con prioridad para los finales.

    Por debajo hay `MAX_CONCURRENT_INFERENCE` trabajadores que toman pedidos
    de una cola priorizada. El numero de trabajadores es deliberadamente chico:
    CTranslate2 con `num_workers=1` ya serializa, asi que tener mas de uno en la
    GPU solo agrega thrash y variabilidad.

    El punto no es rendimiento, es orden. Antes de esto, una sesion con un
    parcial en vuelo y un final recien llegado ejecutaba el parcial primero: el
    usuario esperaba el doble. Ahora el parcial que todavia no empezo se queda
    esperando, y el que ya empezo se deja terminar (cancelarlo a media GPU es
    desperdicio, y el parcial ya esta paying for itself).
    """

    def __init__(self, limit: int = 1) -> None:
        self._limit = max(1, limit)
        self._queue: asyncio.PriorityQueue | None = None
        self._workers: list[asyncio.Task] = []
        self._sequence = 0

    async def start(self) -> None:
        if self._queue is not None:
            return
        self._queue = asyncio.PriorityQueue()
        self._workers = [
            asyncio.create_task(self._worker(), name=f"stt-inference-{index}") for index in range(self._limit)
        ]
        metrics.gauge("stt_inference_workers", self._limit)

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._workers = []
        self._queue = None

    async def run(self, priority: int, func: Callable[..., T], *args: Any) -> T:
        """Ejecuta `func` en un hilo de `to_thread`, cuando le toque por prioridad.

        El trabajo corre en el hilo, no en el bucle: CTranslate2 libera el GIL
        asi que otras conexiones siguen siendo atendidas, y el bucle queda libre
        para seguir leyendo audio, que es lo que evita que se acumule.
        """
        if self._queue is None:
            await self.start()

        assert self._queue is not None
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._sequence += 1
        await self._queue.put((priority, self._sequence, func, args, future))
        metrics.gauge("stt_inference_queue", self._queue.qsize())
        return await future

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            _, _, func, args, future = await self._queue.get()
            if future.cancelled():
                self._queue.task_done()
                continue
            try:
                with ConcurrencyGauge("stt_inference_active", "Inferencias simultaneas en vuelo"):
                    result = await asyncio.to_thread(func, *args)
            except BaseException as exc:  # noqa: BLE001 - se re-lanza al solicitante
                if not future.done():
                    future.set_exception(exc)
            else:
                if not future.done():
                    future.set_result(result)
            finally:
                metrics.gauge("stt_inference_queue", self._queue.qsize())
                self._queue.task_done()


def _loud_frames_rms(audio_buffer: np.ndarray, settings: Settings) -> float:
    frame_size = settings.frame_size
    frame_count = len(audio_buffer) // frame_size
    if frame_count == 0:
        return float(np.sqrt(np.mean(audio_buffer ** 2)))

    frames = audio_buffer[: frame_count * frame_size].reshape(frame_count, frame_size)
    frame_rms = np.sqrt(np.mean(frames ** 2, axis=1))
    return float(np.percentile(frame_rms, settings.MIN_RMS_PERCENTILE))


def transcribe_buffer(
    model: WhisperModel,
    audio_buffer: np.ndarray,
    settings: Settings,
    model_name: str = "",
) -> TranscriptionResult:
    """Transcribe un buffer y devuelve texto, timestamps y diagnostico.

    El filtro de descarte se reporta en vez de desaparecer: `drop_reason` dice
    si el audio estaba en silencio (`rms`), si el modelo lo considera silencio
    (`no_speech`) o si la confianza fue baja (`logprob`). Sin eso, un final
    vacio es indistinguible de un microfono roto.
    """
    audio_ms = len(audio_buffer) / settings.SAMPLE_RATE * 1000
    if len(audio_buffer) == 0:
        metrics.counter("stt_dropped_total", labels={"reason": "empty"})
        return TranscriptionResult("", audio_ms=audio_ms, drop_reason="empty", model=model_name)

    level = _loud_frames_rms(audio_buffer, settings)
    if level < settings.MIN_RMS:
        print(f"[rms] descartado: nivel={level:.4f} umbral={settings.MIN_RMS:.4f}", flush=True)
        metrics.counter("stt_dropped_total", labels={"reason": "rms"})
        return TranscriptionResult("", audio_ms=audio_ms, drop_reason="rms", model=model_name)

    started = time.perf_counter()
    with metrics_timer("stt_inference_ms", "Inferencia Whisper por buffer", {"model": model_name or settings.MODEL_SIZE}):
        segments, _ = model.transcribe(
            audio_buffer,
            language=settings.LANGUAGE,
            beam_size=settings.BEAM_SIZE,
            vad_filter=False,
            condition_on_previous_text=False,
            initial_prompt=settings.INITIAL_PROMPT,
            word_timestamps=settings.WORD_TIMESTAMPS,
        )
        kept: list[str] = []
        words: list[WordTiming] = []
        worst_logprob = 0.0
        worst_no_speech = 0.0

        # transcribe() devuelve un generador perezoso: la decodificacion
        # realmente ocurre aca adentro, no en la llamada.
        for seg in segments:
            worst_logprob = min(worst_logprob, seg.avg_logprob)
            worst_no_speech = max(worst_no_speech, seg.no_speech_prob)
            if seg.no_speech_prob >= settings.NO_SPEECH_THRESHOLD:
                continue
            if seg.avg_logprob < settings.AVG_LOGPROB_THRESHOLD:
                continue

            text = seg.text.strip()
            if text:
                kept.append(text)
            if settings.WORD_TIMESTAMPS:
                words.extend(_word_timings(seg))

    elapsed_ms = (time.perf_counter() - started) * 1000
    result_text = " ".join(kept).strip()

    if not result_text:
        reason = "no_speech" if worst_no_speech >= settings.NO_SPEECH_THRESHOLD else "logprob"
        metrics.counter("stt_dropped_total", labels={"reason": reason, "model": model_name or settings.MODEL_SIZE})
        return TranscriptionResult(
            "",
            audio_ms=audio_ms,
            elapsed_ms=elapsed_ms,
            drop_reason=reason,
            avg_logprob=worst_logprob,
            no_speech_prob=worst_no_speech,
            model=model_name,
        )

    return TranscriptionResult(
        result_text,
        words=words,
        audio_ms=audio_ms,
        elapsed_ms=elapsed_ms,
        avg_logprob=worst_logprob,
        no_speech_prob=worst_no_speech,
        model=model_name,
    )


def _word_timings(segment: Any) -> list[WordTiming]:
    """Convierte `segment.words` de faster-whisper a tiempos locales del buffer.

    Whisper reporta tiempos absolutos dentro de la ventana de 30 s sobre la que
    corrio. Para un buffer corto, esa ventana empieza casi en cero, asi que
    los tiempos son utilizables tal cual; aun asi se resta el inicio del
    segmento para que sirvan si alguna vez el audio tiene relleno al frente.
    """
    if not segment.words:
        return []

    origin = segment.words[0].start
    timings: list[WordTiming] = []
    for word in segment.words:
        text = (word.word or "").strip()
        if not text:
            continue
        start = max(0.0, (word.start or 0.0) - origin)
        end = max(start, (word.end or 0.0) - origin)
        timings.append(WordTiming(text, int(start * 1000), int(end * 1000)))
    return timings