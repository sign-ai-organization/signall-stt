"""Pruebas de `app/main.py`: `/health`, `/metrics`, `/transcribe` y el WebSocket.

El modelo y el planificador se reemplazan por dobles, igual que en
`tests/test_transcriber.py` y en `scripts/verify_session.py`. `TestClient` no
corre el `lifespan` si no se lo usa como context manager, así que Whisper nunca
se carga y las pruebas no necesitan GPU: el estado del servicio
(`app.main.state`) se arma a mano.

Lo que no se prueba: el arranque real (`load_model` + `warm_up`), que descarga
el modelo y compila kernels de CUDA. Eso es hardware, no lógica de la app.
"""

import io
import wave
from dataclasses import replace

import numpy as np
import pytest
from fastapi.testclient import TestClient

import app.main as servicio
from app.config import Settings
from app.metrics import metrics
from app.transcriber import PRIORITY_FINAL, TranscriptionResult, effective_device

settings = Settings()


class FakeVad:
    """VAD por amplitud: voz si el frame supera el umbral."""

    def __init__(self, umbral: float = 0.02) -> None:
        self.umbral = umbral

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        muestras = np.frombuffer(frame, dtype=np.int16)
        rms = float(np.sqrt(np.mean((muestras.astype(np.float32) / 32768.0) ** 2)))
        return rms >= self.umbral


class FakeScheduler:
    """No infiere: contesta un texto fijo y anota con qué prioridad se lo llamó."""

    def __init__(self, texto: str = "hola") -> None:
        self.texto = texto
        self.prioridades: list[int] = []

    async def run(self, priority: int, func, *args) -> TranscriptionResult:
        self.prioridades.append(priority)
        return TranscriptionResult(self.texto, audio_ms=100.0, elapsed_ms=1.0, model="fake")


@pytest.fixture
def cliente():
    """Un cliente HTTP con el estado del servicio armado a mano y sin lifespan."""
    servicio.state.clear()
    servicio.state["model"] = object()
    servicio.state["scheduler"] = FakeScheduler()
    servicio.state["vad"] = FakeVad()
    yield TestClient(servicio.app)
    servicio.state.clear()


def frame_voz(ms: int = 30) -> bytes:
    """Un frame de ruido con amplitud claramente de voz."""
    rng = np.random.default_rng(7)
    muestras = (rng.normal(0.0, 0.3, int(settings.SAMPLE_RATE * ms / 1000)) * 32768).astype(np.int16)
    return muestras.tobytes()


def frame_silencio(ms: int = 30) -> bytes:
    return bytes(int(settings.SAMPLE_RATE * ms / 1000) * 2)


def wav_mono(
    sample_rate: int = settings.SAMPLE_RATE,
    canales: int = 1,
    ancho: int = 2,
    ms: int = 100,
) -> bytes:
    """Un WAV PCM de [ms] milisegundos, armado con la librería estándar."""
    muestras = np.zeros(int(sample_rate * ms / 1000), dtype=np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(canales)
        wav.setsampwidth(ancho)
        wav.setframerate(sample_rate)
        wav.writeframes(muestras.tobytes())
    return buffer.getvalue()


# ── /health ───────────────────────────────────────────────────────────────────


def test_health_dice_el_modelo_y_el_dispositivo(cliente) -> None:
    cuerpo = cliente.get("/health").json()

    assert cuerpo["status"] == "ok"
    assert cuerpo["model"] == settings.MODEL_SIZE
    assert cuerpo["device"] == effective_device(settings.DEVICE)
    assert cuerpo["requested_device"] == settings.DEVICE
    assert cuerpo["language"] == settings.LANGUAGE
    assert cuerpo["silence_timeout_ms"] == settings.SILENCE_TIMEOUT_MS


def test_health_sin_modelo_dice_que_esta_cargando(cliente) -> None:
    servicio.state.clear()

    cuerpo = cliente.get("/health").json()

    assert cuerpo["status"] == "loading"


# ── /metrics y /metrics.json ──────────────────────────────────────────────────


def test_metrics_expone_texto_de_prometheus(cliente) -> None:
    metrics.counter("prueba_endpoint_metrics")

    respuesta = cliente.get("/metrics")

    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"].startswith("text/plain")
    assert "# TYPE prueba_endpoint_metrics counter" in respuesta.text
    assert "prueba_endpoint_metrics 1" in respuesta.text


def test_metrics_json_trae_el_snapshot_y_la_configuracion(cliente) -> None:
    cuerpo = cliente.get("/metrics.json").json()

    assert {"uptime_s", "counters", "gauges", "histograms", "config"} <= set(cuerpo)
    config = cuerpo["config"]
    assert config["model"] == settings.MODEL_SIZE
    assert config["device"] == effective_device(settings.DEVICE)
    assert config["silence_timeout_ms"] == settings.SILENCE_TIMEOUT_MS
    assert config["partial_interval_ms"] == settings.PARTIAL_INTERVAL_MS
    assert config["onset_window_ms"] == settings.SPEECH_ONSET_WINDOW_MS
    assert config["word_timestamps"] is settings.WORD_TIMESTAMPS
    assert config["max_concurrent_inference"] == settings.MAX_CONCURRENT_INFERENCE


# ── POST /transcribe ──────────────────────────────────────────────────────────


def test_transcribe_devuelve_el_texto_del_modelo(cliente) -> None:
    respuesta = cliente.post(
        "/transcribe",
        files={"file": ("audio.wav", wav_mono(), "audio/wav")},
    )

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["text"] == "hola"
    assert cuerpo["audio_ms"] == 100
    assert cuerpo["drop_reason"] is None
    assert cuerpo["words"] == []


def test_transcribe_usa_la_prioridad_de_final(cliente) -> None:
    cliente.post("/transcribe", files={"file": ("audio.wav", wav_mono(), "audio/wav")})

    assert servicio.state["scheduler"].prioridades == [PRIORITY_FINAL]


def test_transcribe_rechaza_un_archivo_que_no_es_wav(cliente) -> None:
    respuesta = cliente.post(
        "/transcribe",
        files={"file": ("notas.txt", b"esto no es un wav", "audio/wav")},
    )

    assert respuesta.status_code == 400
    assert respuesta.json() == {"error": "Archivo WAV inválido"}


def test_transcribe_rechaza_el_audio_que_no_es_mono_pcm16(cliente) -> None:
    respuesta = cliente.post(
        "/transcribe",
        files={"file": ("estereo.wav", wav_mono(canales=2), "audio/wav")},
    )

    assert respuesta.status_code == 400
    assert respuesta.json() == {"error": "Se espera WAV mono PCM16"}


def test_transcribe_rechaza_otro_sample_rate(cliente) -> None:
    respuesta = cliente.post(
        "/transcribe",
        files={"file": ("8000.wav", wav_mono(sample_rate=8000), "audio/wav")},
    )

    assert respuesta.status_code == 400
    assert respuesta.json() == {"error": f"Se espera sample rate de {settings.SAMPLE_RATE} Hz"}


# ── WebSocket /ws/transcribe ──────────────────────────────────────────────────


def test_el_websocket_responde_al_end_aunque_no_haya_audio(cliente) -> None:
    """`flush` contesta siempre: el cliente soltó el micrófono y espera algo."""
    with cliente.websocket_connect("/ws/transcribe") as ws:
        ws.send_text("end")
        mensaje = ws.receive_json()

    assert mensaje["type"] == "final"
    assert mensaje["text"] == ""


def test_el_websocket_ignora_un_texto_que_no_es_end(cliente) -> None:
    with cliente.websocket_connect("/ws/transcribe") as ws:
        ws.send_text("hola")
        ws.send_bytes(b"")   # audio vacío: no dispara nada
        ws.send_text("END")  # `end` no distingue mayúsculas
        mensaje = ws.receive_json()

    assert mensaje["type"] == "final"


def test_el_websocket_cierra_la_frase_por_silencio(cliente, monkeypatch) -> None:
    # Sin parciales: el parcial se pide contra el reloj de pared y en la prueba
    # llegaría como un mensaje más, que no es lo que se está mirando.
    monkeypatch.setattr(servicio, "settings", replace(settings, PARTIAL_INTERVAL_MS=0))

    with cliente.websocket_connect("/ws/transcribe") as ws:
        for _ in range(10):  # 300 ms de voz: llena la ventana de inicio
            ws.send_bytes(frame_voz())
        for _ in range(50):  # 1500 ms de silencio: cierra la frase
            ws.send_bytes(frame_silencio())
        mensaje = ws.receive_json()

    assert mensaje["type"] == "final"
    assert mensaje["text"] == "hola"
    assert mensaje["audio_ms"] > 0
    assert servicio.state["scheduler"].prioridades == [PRIORITY_FINAL]
