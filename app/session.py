"""Estado de streaming por conexión: buffering + VAD + decisión partial/final.

Reemplaza el par de hilos + globals compartidos del script original por una
sesión aislada por cliente, ya que el servicio puede atender varias
conexiones WebSocket concurrentes (una por cada cliente móvil).
"""

import asyncio
import time
from collections import deque

import numpy as np
import webrtcvad
from faster_whisper import WhisperModel

from app.config import Settings
from app.transcriber import transcribe_buffer


class StreamingSession:
    """Estado aislado por conexión WebSocket (sin globals compartidos entre clientes)."""

    def __init__(self, model: WhisperModel, vad: webrtcvad.Vad, settings: Settings):
        self._model = model
        self._vad = vad
        self._settings = settings

        self._pcm_buffer = bytearray()
        self._speech_buffer = np.zeros(0, dtype=np.float32)
        self._is_speaking = False
        self._silence_ms = 0
        self._last_partial_time = time.time()

        self._first_chunk_at: float | None = None
        self._processed_audio_ms = 0
        self._speech_end_audio_ms = 0

        onset_frames = max(1, settings.SPEECH_ONSET_WINDOW_MS // settings.FRAME_DURATION_MS)
        self._onset_window: deque[tuple[np.ndarray, bool]] = deque(maxlen=onset_frames)

        # Parciales en segundo plano
        self._utterance_id = 0
        self._partial_task: asyncio.Task | None = None
        self._ready_partial: dict | None = None
        self._speech_since_partial = False

    async def feed(self, chunk: bytes) -> list[dict]:
        if self._first_chunk_at is None:
            self._first_chunk_at = time.monotonic()
        self._pcm_buffer.extend(chunk)
        results = []

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

    async def flush(self) -> dict | None:
        if len(self._speech_buffer) == 0:
            return None

        buffer_copy = self._trim_trailing_silence(self._speech_buffer, self._silence_ms)
        self._speech_buffer = np.zeros(0, dtype=np.float32)
        self._is_speaking = False
        self._silence_ms = 0
        self._close_utterance()

        text = await asyncio.to_thread(transcribe_buffer, self._model, buffer_copy, self._settings)
        return {"type": "final", "text": text} if text else None

    async def _process_frame(self, frame_bytes: bytes) -> dict | None:
        frame_int16 = np.frombuffer(frame_bytes, dtype=np.int16)
        frame_float = frame_int16.astype(np.float32) / 32768.0

        try:
            speech_detected = self._vad.is_speech(frame_bytes, self._settings.SAMPLE_RATE)
        except Exception:
            speech_detected = False

        self._processed_audio_ms += self._settings.FRAME_DURATION_MS

        if not self._is_speaking:
            self._detect_speech_onset(frame_float, speech_detected)
        elif speech_detected:
            self._speech_buffer = np.concatenate([self._speech_buffer, frame_float])
            self._silence_ms = 0
            self._speech_since_partial = True
        else:
            if self._silence_ms == 0:
                self._speech_end_audio_ms = self._processed_audio_ms - self._settings.FRAME_DURATION_MS
            self._speech_buffer = np.concatenate([self._speech_buffer, frame_float])
            self._silence_ms += self._settings.FRAME_DURATION_MS

        buffer_ms = len(self._speech_buffer) / self._settings.SAMPLE_RATE * 1000
        now = time.time()

        do_final = self._is_speaking and (
            self._silence_ms >= self._settings.SILENCE_TIMEOUT_MS
            or buffer_ms >= self._settings.MAX_BUFFER_MS
        )
        if do_final:
            reason = "silencio" if self._silence_ms >= self._settings.SILENCE_TIMEOUT_MS else "buffer_max"
            buffer_copy = self._trim_trailing_silence(self._speech_buffer, self._silence_ms)
            self._speech_buffer = np.zeros(0, dtype=np.float32)
            self._is_speaking = False
            self._silence_ms = 0
            self._close_utterance()

            transcribed_ms = len(buffer_copy) / self._settings.SAMPLE_RATE * 1000
            lag_ms = self._lag_ms()
            started = time.monotonic()
            text = await asyncio.to_thread(transcribe_buffer, self._model, buffer_copy, self._settings)
            transcribe_ms = (time.monotonic() - started) * 1000
            self._log_final(reason, transcribed_ms, transcribe_ms, lag_ms)
            return {"type": "final", "text": text} if text else None

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
                self._run_partial(self._speech_buffer.copy(), buffer_ms, self._utterance_id)
            )

        return None

    def _detect_speech_onset(self, frame_float: np.ndarray, speech_detected: bool) -> None:
        self._onset_window.append((frame_float, speech_detected))
        speech_frames = sum(1 for _, is_speech in self._onset_window if is_speech)
        window_full = len(self._onset_window) == self._onset_window.maxlen
        if not window_full or speech_frames < self._settings.SPEECH_ONSET_RATIO * self._onset_window.maxlen:
            return

        self._speech_buffer = np.concatenate([frame for frame, _ in self._onset_window])
        self._onset_window.clear()
        self._is_speaking = True
        self._silence_ms = 0
        self._speech_since_partial = True

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

    async def _run_partial(self, buffer_copy: np.ndarray, buffer_ms: float, utterance_id: int) -> None:
        lag_ms = self._lag_ms()
        started = time.monotonic()
        try:
            text = await asyncio.to_thread(transcribe_buffer, self._model, buffer_copy, self._settings)
        except Exception as exc:
            print(f"[ERROR] transcripción parcial falló: {exc}", flush=True)
            return
        finally:
            self._last_partial_time = time.time()

        transcribe_ms = (time.monotonic() - started) * 1000
        print(
            f"[timing] parcial: audio={buffer_ms / 1000:.1f}s "
            f"transcripcion={transcribe_ms:.0f}ms atraso={lag_ms:.0f}ms",
            flush=True,
        )
        if text and utterance_id == self._utterance_id:
            self._ready_partial = {"type": "partial", "text": text}

    def _lag_ms(self) -> float:
        elapsed_ms = (time.monotonic() - self._first_chunk_at) * 1000
        return elapsed_ms - self._processed_audio_ms

    def _log_final(self, reason: str, buffer_ms: float, transcribe_ms: float, lag_ms: float) -> None:
        line = (
            f"[timing] final ({reason}): audio={buffer_ms / 1000:.1f}s "
            f"transcripcion={transcribe_ms:.0f}ms atraso={lag_ms:.0f}ms"
        )
        if reason == "silencio":
            speech_end_at = self._first_chunk_at + self._speech_end_audio_ms / 1000
            end_to_final_ms = (time.monotonic() - speech_end_at) * 1000
            line += f" fin_habla->final={end_to_final_ms:.0f}ms"
        print(line, flush=True)
