import asyncio
import io
import wave
from contextlib import asynccontextmanager

import numpy as np
import webrtcvad
from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from app.config import settings
from app.session import StreamingSession
from app.transcriber import load_model, transcribe_buffer

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["model"] = load_model(settings)
    state["vad"] = webrtcvad.Vad(settings.VAD_AGGRESSIVENESS)
    yield
    state.clear()


app = FastAPI(title="SignAI STT Service", lifespan=lifespan)


@app.get("/health")
def health():
    return {
        "status": "ok" if "model" in state else "loading",
        "model": settings.MODEL_SIZE,
        "device": settings.DEVICE,
        "language": settings.LANGUAGE,
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
    text = await asyncio.to_thread(transcribe_buffer, state["model"], audio, settings)
    return {"text": text}


@app.websocket("/ws/transcribe")
async def ws_transcribe(websocket: WebSocket):
    await websocket.accept()
    session = StreamingSession(state["model"], state["vad"], settings)

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
