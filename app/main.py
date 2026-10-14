import io
import wave
from contextlib import asynccontextmanager

import numpy as np
import webrtcvad
from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import settings
from app.metrics import metrics
from app.session import StreamingSession
from app.transcriber import PRIORITY_FINAL, InferenceScheduler, load_models, transcribe_buffer

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    models = load_models(settings)
    state["models"] = models
    state["scheduler"] = InferenceScheduler(settings.MAX_CONCURRENT_INFERENCE)
    await state["scheduler"].start()
    state["vad"] = webrtcvad.Vad(settings.VAD_AGGRESSIVENESS)
    metrics.gauge("stt_ready", 1)
    yield
    metrics.gauge("stt_ready", 0)
    await state["scheduler"].stop()
    state.clear()


app = FastAPI(title="SignAI STT Service", lifespan=lifespan)


@app.get("/health")
def health():
    return {
        "status": "ok" if state.get("models") else "loading",
        "model": settings.MODEL_SIZE,
        "partial_model": settings.partial_model_size,
        "device": settings.DEVICE,
        "language": settings.LANGUAGE,
        "realtime": settings.SILENCE_TIMEOUT_MS < 1500,
    }


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics():
    """Metricas en formato de exposicion de Prometheus.

    `curl localhost:8000/metrics` es el chequeo rapido. Para lo estructurado
    esta `/metrics.json`: las pruebas automaticas leen ahi porque parsear texto
    con expresiones regulares es fragil.
    """
    return metrics.render()


@app.get("/metrics.json")
def metrics_json():
    return {
        **metrics.snapshot(),
        "config": {
            "model": settings.MODEL_SIZE,
            "partial_model": settings.partial_model_size,
            "device": settings.DEVICE,
            "silence_timeout_ms": settings.SILENCE_TIMEOUT_MS,
            "partial_interval_ms": settings.PARTIAL_INTERVAL_MS,
            "onset_window_ms": settings.SPEECH_ONSET_WINDOW_MS,
            "word_timestamps": settings.WORD_TIMESTAMPS,
            "max_concurrent_inference": settings.MAX_CONCURRENT_INFERENCE,
        },
    }


@app.post("/transcribe")
async def transcribe_file(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        with wave.open(io.BytesIO(raw), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                return JSONResponse(
                    {"error": "Se espera WAV mono PCM16"}, status_code=400
                )
            if wf.getframerate() != settings.SAMPLE_RATE:
                return JSONResponse(
                    {"error": f"Se espera sample rate de {settings.SAMPLE_RATE} Hz"},
                    status_code=400,
                )
            pcm = wf.readframes(wf.getnframes())
    except wave.Error:
        return JSONResponse({"error": "Archivo WAV inválido"}, status_code=400)

    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    result = await state["scheduler"].run(
        PRIORITY_FINAL,
        transcribe_buffer,
        state["models"]["final"],
        audio,
        settings,
        settings.MODEL_SIZE,
    )
    return {
        "text": result.text,
        "audio_ms": round(result.audio_ms),
        "elapsed_ms": round(result.elapsed_ms),
        "drop_reason": result.drop_reason,
        "words": [word.to_dict() for word in result.words],
    }


@app.websocket("/ws/transcribe")
async def ws_transcribe(websocket: WebSocket):
    await websocket.accept()
    session = StreamingSession(state["models"], state["scheduler"], state["vad"], settings)
    metrics.gauge("stt_sessions_active", metrics.counter_value("stt_sessions_active") + 1)

    try:
        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            data = message.get("bytes")
            text_msg = message.get("text")

            if data:
                for result in await session.feed(data):
                    await websocket.send_json(result)
            elif text_msg is not None and text_msg.strip().lower() == "end":
                result = await session.flush()
                if result:
                    await websocket.send_json(result)
    except WebSocketDisconnect:
        pass
    finally:
        metrics.gauge("stt_sessions_active", max(0.0, metrics.counter_value("stt_sessions_active") - 1))
        metrics.counter("stt_sessions_total")