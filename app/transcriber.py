"""Carga del modelo Whisper y transcripción de buffers de audio."""

import time

import numpy as np
from faster_whisper import WhisperModel

from app.config import Settings


def load_model(settings: Settings) -> WhisperModel:
    print(f"Cargando modelo {settings.MODEL_SIZE} ({settings.DEVICE}, {settings.COMPUTE_TYPE})...")
    try:
        model = WhisperModel(settings.MODEL_SIZE, device=settings.DEVICE, compute_type=settings.COMPUTE_TYPE)
        print(f"Modelo cargado en {settings.DEVICE}.")
        return model
    except Exception as exc:
        if settings.DEVICE == "cuda":
            print(f"[ADVERTENCIA] GPU no disponible: {exc}\nCayendo a CPU...")
            model = WhisperModel(settings.MODEL_SIZE, device="cpu", compute_type="int8")
            print("Modelo cargado en CPU.")
            return model
        raise


def warm_up(model: WhisperModel, settings: Settings) -> None:
    started = time.monotonic()
    silence = np.zeros(settings.SAMPLE_RATE, dtype=np.float32)
    segments, _ = model.transcribe(silence, language=settings.LANGUAGE, beam_size=settings.BEAM_SIZE)
    list(segments)  # transcribe() es perezoso
    print(f"Modelo precalentado en {(time.monotonic() - started) * 1000:.0f} ms.", flush=True)


def _loud_frames_rms(audio_buffer: np.ndarray, settings: Settings) -> float:
    frame_size = settings.frame_size
    frame_count = len(audio_buffer) // frame_size
    if frame_count == 0:
        return float(np.sqrt(np.mean(audio_buffer ** 2)))

    frames = audio_buffer[: frame_count * frame_size].reshape(frame_count, frame_size)
    frame_rms = np.sqrt(np.mean(frames ** 2, axis=1))
    return float(np.percentile(frame_rms, settings.MIN_RMS_PERCENTILE))


def transcribe_buffer(model: WhisperModel, audio_buffer: np.ndarray, settings: Settings) -> str:
    if len(audio_buffer) == 0:
        return ""

    level = _loud_frames_rms(audio_buffer, settings)
    if level < settings.MIN_RMS:
        print(f"[rms] descartado: nivel={level:.4f} umbral={settings.MIN_RMS:.4f}", flush=True)
        return ""

    segments, _ = model.transcribe(
        audio_buffer,
        language=settings.LANGUAGE,
        beam_size=settings.BEAM_SIZE,
        vad_filter=False,
        condition_on_previous_text=False,
        initial_prompt=settings.INITIAL_PROMPT,
    )
    kept = []
    for seg in segments:
        if seg.no_speech_prob >= settings.NO_SPEECH_THRESHOLD:
            continue
        if seg.avg_logprob < settings.AVG_LOGPROB_THRESHOLD:
            continue
        kept.append(seg.text.strip())
    return " ".join(kept).strip()
