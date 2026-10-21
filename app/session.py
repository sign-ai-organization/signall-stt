"""Estado de streaming por conexión: buffering + VAD + decisión partial/final.

Reemplaza el par de hilos + globals compartidos del script original por una
sesión aislada por cliente, ya que el servicio puede atender varias conexiones
WebSocket concurrentes (una por cada cliente móvil).

Dos decisiones de este archivo no dependen de con qué ritmo hable la persona:

1. **El buffer crece con `list.append`, no con `np.concatenate`.** La versión
   anterior concatenaba el frame de 30 ms contra el buffer completo en cada
   frame: para 8 s de habla son ~270 concatenaciones de un arreglo que llega a
   128k muestras, o sea ~68 MB de memcpy, y todo en el hilo del bucle de
   eventos. Eso bloqueaba la VAD de *todas* las conexiones, no solo la propia.
   Ahora se acumula en una lista y se junta una sola vez, cuando toca
   transcribir.

2. **Un parcial en vuelo no se entrega si su frase ya cerró.** Cuando el
   utterance se cierra, el parcial que estaba corriendo describe un buffer viejo
   y su texto confundiría al backend, así que se descarta. Además se cancela si
   todavía no llegó a la GPU, para que no quede encolado delante del final.

Lo que este archivo **no** decide es qué se representa: eso es del backend, que
divide la frase en oraciones cuando el cliente se la manda como `text`.
"""

import asyncio
import time
from collections import deque

import numpy as np
import webrtcvad
from faster_whisper import WhisperModel

from app.config import Settings
from app.metrics import metrics, metrics_timer
from app.transcriber import (
    PRIORITY_FINAL,
    PRIORITY_PARTIAL,
    InferenceScheduler,
    TranscriptionResult,
    transcribe_buffer,
)


class StreamingSession:
    """Estado aislado por conexión WebSocket (sin globals compartidos entre clientes)."""

    def __init__(
        self,
        model: WhisperModel,
        scheduler: InferenceScheduler,
        vad: webrtcvad.Vad,
        settings: Settings,
    ):
        self._model = model
        self._scheduler = scheduler
        self._vad = vad
        self._settings = settings

        self._pcm_buffer = bytearray()
        # Tramos del utterance en curso. Se juntan una sola vez al transcribir.
        self._speech_chunks: list[np.ndarray] = []
        self._speech_samples = 0
        self._frame_flags: list[bool] = []
        self._is_speaking = False
        self._silence_ms = 0
        self._last_partial_time = time.monotonic()

        self._received_audio_ms = 0
        # Foto del audio recibido en el instante en que arrancó la última
        # inferencia. Es la referencia de `_lag_ms`.
        self._backlog_snapshot_ms: float | None = None
        # Reloj de pared del último frame que el VAD marcó como voz. Es la
        # referencia de `stt_end_to_final_ms` (ver `_log_final`).
        self._speech_end_at: float | None = None

        onset_frames = max(1, settings.SPEECH_ONSET_WINDOW_MS // settings.FRAME_DURATION_MS)
        self._onset_window: deque[tuple[np.ndarray, bool]] = deque(maxlen=onset_frames)

        # Parciales en segundo plano
        self._utterance_id = 0
        self._partial_task: asyncio.Task | None = None
        self._ready_partial: dict | None = None
        self._speech_since_partial = False

    # ---------------------------------------------------------------- audio

    async def feed(self, chunk: bytes) -> list[dict]:
        self._pcm_buffer.extend(chunk)
        results = []
        metrics.counter("stt_audio_bytes_total", len(chunk))

        # El parcial listo se entrega antes de procesar audio nuevo
        if self._ready_partial is not None:
            results.append(self._ready_partial)
            self._ready_partial = None

        bpf = self._settings.bytes_per_frame
        while len(self._pcm_buffer) >= bpf:
            frame_bytes = bytes(self._pcm_buffer[:bpf])
            del self._pcm_buffer[:bpf]
            result = await self._process_frame(frame_bytes)
            if result:
                results.append(result)

        return results

    async def flush(self) -> dict:
        """Cierra el utterance actual por pedido explícito del cliente.

        Es el atajo de baja latencia: el cliente (el botón del micrófono) avisa
        que soltó y se salta el hangover de silencio. Sin esto, soltar el
        micrófono a mitad de frase obligaba a esperar `SILENCE_TIMEOUT_MS` a un
        silencio que ya no iba a llegar.

        **Este es el único camino que responde siempre**, vacío incluido, porque
        es el único en el que el cliente preguntó. Los dos casos por los que
        antes se callaba son los dos casos normales de soltar el micrófono:

        - El filtro de descarte se comió el audio (`no_speech`, `logprob`,
          `rms`). Es lo que pasa al soltar el micrófono sobre ruido: la frase se
          cerró y no tenía palabras, y eso es una respuesta.
        - Ya no queda audio pendiente porque el silencio la cerró antes. El `end`
          del cliente llega tarde, no en balde.

        Antes, en los dos casos, no salía nada y quien se quedaba esperando era el
        cliente: su red de seguridad ("no llegó la transcripción") reportaba como
        perdida una frase que nunca existió. Un final vacío es una respuesta
        válida y el backend ya la reenvía tal cual.

        El camino por silencio (`feed`) sigue descartando los vacíos: ahí nadie
        está preguntando y mandarlos sería ruido en la conexión.
        """
        if self._speech_samples == 0:
            result = TranscriptionResult("", drop_reason="empty")
            self._log_final("flush", result, self._lag_ms())
            return result.to_payload("final")

        buffer_copy = self._trim_trailing_silence(self._speech_audio(), self._silence_ms)
        self._end_speech()

        result = await self._transcribe(buffer_copy, priority=PRIORITY_FINAL)
        self._log_final("flush", result, self._lag_ms())
        return result.to_payload("final")

    # -------------------------------------------------------------uttokens

    async def _process_frame(self, frame_bytes: bytes) -> dict | None:
        frame_int16 = np.frombuffer(frame_bytes, dtype=np.int16)
        frame_float = frame_int16.astype(np.float32) / 32768.0

        with metrics_timer("stt_vad_ms", "Costo del VAD por frame de 30 ms"):
            try:
                vad_speech = self._vad.is_speech(frame_bytes, self._settings.SAMPLE_RATE)
            except Exception:
                vad_speech = False
            frame_rms = float(np.sqrt(np.mean(frame_float ** 2)))
        speech_detected = vad_speech and frame_rms >= self._settings.MIN_RMS

        self._received_audio_ms += self._settings.FRAME_DURATION_MS
        now = time.monotonic()

        if not self._is_speaking:
            self._detect_speech_onset(frame_float, speech_detected, now)
        elif speech_detected:
            self._append_speech(frame_float)
            self._frame_flags.append(True)
            self._silence_ms = 0
            self._speech_since_partial = True
            self._speech_end_at = now
        else:
            self._append_speech(frame_float)
            self._frame_flags.append(False)
            self._silence_ms += self._settings.FRAME_DURATION_MS

        buffer_ms = self._speech_samples / self._settings.SAMPLE_RATE * 1000

        do_final = self._is_speaking and (
            self._silence_ms >= self._settings.SILENCE_TIMEOUT_MS
            or buffer_ms >= self._settings.MAX_BUFFER_MS
        )
        if do_final:
            audio, reason = self._take_final_audio()
            lag_ms = self._lag_ms()
            # El parcial en vuelo ya no sirve para nada: el utterance cerró y su
            # texto, si llega, va a describir un buffer viejo. Tirarlo libera la
            # GPU para el final, que es lo que el usuario está esperando.
            self._cancel_partial()
            result = await self._transcribe(audio, priority=PRIORITY_FINAL)
            self._log_final(reason, result, lag_ms)
            # El silencio lo cerró solo: nadie preguntó, así que un final vacío
            # no se manda. `flush()` es el camino que sí responde siempre.
            return result.to_payload("final") if result.text else None

        do_partial = (
            self._settings.PARTIAL_INTERVAL_MS > 0
            and self._is_speaking
            and self._speech_since_partial
            and (self._partial_task is None or self._partial_task.done())
            and buffer_ms >= self._settings.MIN_AUDIO_MS_FOR_PARTIAL
            and (now - self._last_partial_time) * 1000 >= self._settings.PARTIAL_INTERVAL_MS
        )
        if do_partial:
            self._speech_since_partial = False
            self._partial_task = asyncio.create_task(
                self._run_partial(self._speech_audio(), buffer_ms, self._utterance_id),
                name=f"stt-partial-{self._utterance_id}",
            )

        return None

    def _cancel_partial(self) -> None:
        """Cancela el parcial en vuelo si todavia no termino.

        Cancelar el `await` descarta el resultado; el hilo de la GPU sigue
        hasta que CTranslate2 termine, porque ahi no se puede interrumpir a
        mitad. Aun asi vale la pena: lo que se gana es no entregar un texto
        viejo, y no encolar el siguiente parcial detras de una inferencia que ya
        no interesa.
        """
        task = self._partial_task
        if task is not None and not task.done():
            task.cancel()
            metrics.counter("stt_partial_cancelled_total")

    def _append_speech(self, frame_float: np.ndarray) -> None:
        self._speech_chunks.append(frame_float)
        self._speech_samples += frame_float.size

    def _speech_audio(self) -> np.ndarray:
        """Junta los tramos acumulados. O(n), y solo cuando hay que transcribir."""
        if not self._speech_chunks:
            return np.zeros(0, dtype=np.float32)
        if len(self._speech_chunks) == 1:
            return self._speech_chunks[0]
        audio = np.concatenate(self._speech_chunks)
        self._speech_chunks = [audio]
        return audio

    def _detect_speech_onset(
        self,
        frame_float: np.ndarray,
        speech_detected: bool,
        now: float,
    ) -> None:
        self._onset_window.append((frame_float, speech_detected))
        speech_frames = sum(1 for _, is_speech in self._onset_window if is_speech)
        window_full = len(self._onset_window) == self._onset_window.maxlen
        if not window_full or speech_frames < self._settings.SPEECH_ONSET_RATIO * self._onset_window.maxlen:
            return

        for frame, _ in self._onset_window:
            self._append_speech(frame)
        self._frame_flags = [is_speech for _, is_speech in self._onset_window]
        # Los frames de la ventana son los únicos que traversed esta rama, así
        # que el reloj de "dejaste de hablar" se actualiza acá. Solo si el frame
        # actual es voz: si la ventana cerró en silencio, el último frame de voz
        # llegó hasta `SPEECH_ONSET_WINDOW_MS` antes y no hay con qué ser exacto.
        if speech_detected:
            self._speech_end_at = now
        self._onset_window.clear()
        self._is_speaking = True
        self._silence_ms = 0
        self._speech_since_partial = True

    def _take_final_audio(self) -> tuple[np.ndarray, str]:
        if self._silence_ms >= self._settings.SILENCE_TIMEOUT_MS:
            audio = self._trim_trailing_silence(self._speech_audio(), self._silence_ms)
            self._end_speech()
            return audio, "silencio"

        cut_frame = self._find_pause_cut()
        if cut_frame is None:
            audio = self._speech_audio()
            self._end_speech()
            return audio, "buffer_max_forzado"

        # La frase sigue en curso: lo que va después de la pausa inicia la siguiente.
        # `_speech_audio()` colapsa los tramos en uno, asi que el corte es un
        # slice sobre un solo arreglo en vez de uno por tramo.
        full = self._speech_audio()
        cut_sample = cut_frame * self._settings.frame_size
        audio = full[:cut_sample]
        self._speech_chunks = [full[cut_sample:]] if cut_sample < full.size else []
        self._speech_samples = max(0, self._speech_samples - cut_sample)
        self._frame_flags = self._frame_flags[cut_frame:]
        self._close_utterance()
        self._speech_since_partial = True
        return audio, "buffer_max_pausa"

    def _find_pause_cut(self) -> int | None:
        min_pause_frames = max(1, self._settings.CUT_PAUSE_MIN_MS // self._settings.FRAME_DURATION_MS)
        earliest = len(self._frame_flags) // 2
        i = len(self._frame_flags) - 1
        while i >= earliest:
            if self._frame_flags[i]:
                i -= 1
                continue
            pause_end = i
            while i >= earliest and not self._frame_flags[i]:
                i -= 1
            if pause_end - i >= min_pause_frames:
                return (i + 1 + pause_end + 1) // 2
        return None

    def _end_speech(self) -> None:
        self._speech_chunks = []
        self._speech_samples = 0
        self._frame_flags = []
        self._is_speaking = False
        self._silence_ms = 0
        self._close_utterance()

    def _trim_trailing_silence(self, buffer: np.ndarray, silence_ms: int) -> np.ndarray:
        trim_ms = silence_ms - self._settings.FINAL_TRAILING_SILENCE_KEEP_MS
        if trim_ms <= 0:
            return buffer
        trim_samples = int(self._settings.SAMPLE_RATE * trim_ms / 1000)
        return buffer[:-trim_samples]

    def _close_utterance(self) -> None:
        self._utterance_id += 1
        self._ready_partial = None
        self._speech_since_partial = False

    # --------------------------------------------------------- transcripcion

    async def _transcribe(self, audio: np.ndarray, priority: int) -> TranscriptionResult:
        # La foto se toma acá, cuando la inferencia **empieza** a correr, y no
        # cuando se despacha: la espera por la GPU ya la mide
        # `stt_partial_queue_ms`, y sumar las dos sería contar el atraso dos
        # veces.
        self._backlog_snapshot_ms = self._received_audio_ms
        return await self._scheduler.run(
            priority,
            transcribe_buffer,
            self._model,
            audio,
            self._settings,
            self._settings.MODEL_SIZE,
        )

    async def _run_partial(self, buffer_copy: np.ndarray, buffer_ms: float, utterance_id: int) -> None:
        lag_ms = self._lag_ms()
        queued_at = time.perf_counter()
        try:
            result = await self._transcribe(buffer_copy, priority=PRIORITY_PARTIAL)
        except asyncio.CancelledError:
            metrics.counter("stt_partial_cancelled_total")
            raise
        except Exception as exc:
            print(f"[ERROR] transcripción parcial falló: {exc}", flush=True)
            metrics.counter("stt_partial_failed_total")
            return
        finally:
            self._last_partial_time = time.monotonic()

        queue_ms = (time.perf_counter() - queued_at) * 1000 - result.elapsed_ms
        print(
            f"[timing] parcial: audio={buffer_ms / 1000:.1f}s "
            f"transcripcion={result.elapsed_ms:.0f}ms espera_gpu={max(0.0, queue_ms):.0f}ms "
            f"atraso={lag_ms:.0f}ms texto={result.text!r}",
            flush=True,
        )
        metrics.histogram("stt_partial_queue_ms", max(0.0, queue_ms), "Espera del parcial por la GPU")
        metrics.histogram(
            "stt_partial_ms",
            result.elapsed_ms,
            "Inferencia de un parcial",
            {"model": result.model or "?"},
        )
        metrics.histogram("stt_partial_lag_ms", lag_ms, "Audio acumulado sin procesar")
        metrics.counter("stt_partial_total", labels={"kept": "1" if result.text else "0"})

        # El utterance pudo cerrar mientras este parcial corria: lo que diga ya
        # es del pasado y confundiria al backend, asi que se descarta.
        if result.text and utterance_id == self._utterance_id:
            self._ready_partial = result.to_payload("partial")
        else:
            metrics.counter("stt_partial_stale_total")

    # -------------------------------------------------------------- metricas

    def _lag_ms(self) -> float:
        """Cuánto audio llegó mientras se transcribía y todavía no se transcribió.

        Es la única medida honesta de saturación, y la definición importa: **audio
        recibido menos el audio que ya estaba en el buffer cuando empezó la
        inferencia**.

        Antes se medía contra el reloj de la sesión (tiempo real desde el primer
        frame menos audio recibido), lo que mezclaba dos cosas sin relación: con
        el micrófono apagado 20 s el número llegaba a 20000 ms y parecía una GPU
        saturada, cuando en realidad no estaba llegando nada que procesar. Y al
        reproducir más rápido que tiempo real salía negativo. En una prueba con
        voz real, p95=13091 y max=27332 eran casi todos segundos de micrófono
        apagado, no atraso.

        Ahora mide lo único que importa: si el avatar va "atrás" respecto de la
        voz. Cero significa que la transcripción le gana al habla; si sube, la
        GPU o el modelo no dan abasto y hay que mirar `stt_inference_ms` y
        `stt_partial_queue_ms` para saber cuál de los dos.
        """
        if self._backlog_snapshot_ms is None:
            return 0.0
        return max(0.0, self._received_audio_ms - self._backlog_snapshot_ms)

    def _log_final(self, reason: str, result: TranscriptionResult, lag_ms: float) -> None:
        line = (
            f"[timing] final ({reason}): audio={result.audio_ms / 1000:.1f}s "
            f"transcripcion={result.elapsed_ms:.0f}ms atraso={lag_ms:.0f}ms"
        )
        if self._speech_end_at is not None:
            end_to_final_ms = (time.monotonic() - self._speech_end_at) * 1000
            line += f" fin_habla->final={end_to_final_ms:.0f}ms"
            metrics.histogram(
                "stt_end_to_final_ms",
                end_to_final_ms,
                "Dejar de hablar a final disponible",
                {"reason": reason},
            )
        if result.drop_reason:
            line += f" descartado={result.drop_reason}"

        metrics.histogram(
            "stt_final_ms",
            result.elapsed_ms,
            "Inferencia de un final",
            {"model": result.model or "?"},
        )
        metrics.histogram("stt_final_lag_ms", lag_ms, "Audio acumulado sin procesar al cerrar")
        metrics.counter("stt_final_total", labels={"reason": reason, "kept": "1" if result.text else "0"})
        print(line + f" texto={result.text!r}", flush=True)