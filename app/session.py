"""Estado de streaming por conexión: buffering + VAD + decisión partial/final.

Reemplaza el par de hilos + globals compartidos del script original por una
sesión aislada por cliente, ya que el servicio puede atender varias
conexiones WebSocket concurrentes (una por cada cliente móvil).
"""

import asyncio
import time

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

    async def feed(self, chunk: bytes) -> list[dict]:
        self._pcm_buffer.extend(chunk)
        results = []

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

        buffer_copy = self._speech_buffer
        self._speech_buffer = np.zeros(0, dtype=np.float32)
        self._is_speaking = False
        self._silence_ms = 0

        text = await asyncio.to_thread(transcribe_buffer, self._model, buffer_copy, self._settings)
        return {"type": "final", "text": text} if text else None

    async def _process_frame(self, frame_bytes: bytes) -> dict | None:
        frame_int16 = np.frombuffer(frame_bytes, dtype=np.int16)
        frame_float = frame_int16.astype(np.float32) / 32768.0

        try:
            speech_detected = self._vad.is_speech(frame_bytes, self._settings.SAMPLE_RATE)
        except Exception:
            speech_detected = False

        if speech_detected:
            self._speech_buffer = np.concatenate([self._speech_buffer, frame_float])
            self._silence_ms = 0
            self._is_speaking = True
        elif self._is_speaking:
            self._speech_buffer = np.concatenate([self._speech_buffer, frame_float])
            self._silence_ms += self._settings.FRAME_DURATION_MS

        buffer_ms = len(self._speech_buffer) / self._settings.SAMPLE_RATE * 1000
        now = time.time()

        do_final = self._is_speaking and (
            self._silence_ms >= self._settings.SILENCE_TIMEOUT_MS
            or buffer_ms >= self._settings.MAX_BUFFER_MS
        )
        if do_final:
            buffer_copy = self._speech_buffer
            self._speech_buffer = np.zeros(0, dtype=np.float32)
            self._is_speaking = False
            self._silence_ms = 0

            text = await asyncio.to_thread(transcribe_buffer, self._model, buffer_copy, self._settings)
            return {"type": "final", "text": text}

        do_partial = (
            self._is_speaking
            and buffer_ms >= self._settings.MIN_AUDIO_MS_FOR_PARTIAL
            and (now - self._last_partial_time) * 1000 >= self._settings.PARTIAL_INTERVAL_MS
        )
        if do_partial:
            self._last_partial_time = now
            buffer_copy = self._speech_buffer.copy()

            text = await asyncio.to_thread(transcribe_buffer, self._model, buffer_copy, self._settings)
            if text:
                return {"type": "partial", "text": text}

        return None
