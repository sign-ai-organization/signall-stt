"""Carga del modelo Whisper y transcripción de buffers de audio."""

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


def transcribe_buffer(model: WhisperModel, audio_buffer: np.ndarray, settings: Settings) -> str:
    if len(audio_buffer) == 0:
        return ""

    rms = float(np.sqrt(np.mean(audio_buffer ** 2)))
    if rms < settings.MIN_RMS:
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
