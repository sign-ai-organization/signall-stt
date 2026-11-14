"""Pruebas de `transcribe_buffer` con un modelo falso.

No cargan Whisper ni audio real: lo que se prueba es qué decide el servicio
sobre lo que el modelo devuelve —qué texto se conserva, qué se descarta y con
qué `drop_reason`—, no la calidad de la transcripción. Es la red que protege la
lógica que quedó partida en `_collect_segments`.
"""

from dataclasses import dataclass, replace

import numpy as np

from app.config import Settings
from app.transcriber import transcribe_buffer


@dataclass
class FakeWord:
    word: str
    start: float
    end: float


@dataclass
class FakeSegment:
    text: str
    avg_logprob: float = -0.2
    no_speech_prob: float = 0.1
    words: list[FakeWord] | None = None


class FakeModel:
    """Devuelve siempre los mismos segmentos y recuerda con qué se lo llamó."""

    def __init__(self, *segments: FakeSegment) -> None:
        self.segments = list(segments)
        self.kwargs: dict = {}

    def transcribe(self, audio: np.ndarray, **kwargs):
        self.kwargs = kwargs
        return iter(self.segments), None


def audio_de_voz(settings: Settings, segundos: float = 0.5) -> np.ndarray:
    """Ruido con amplitud de voz: pasa el filtro de RMS."""
    rng = np.random.default_rng(3)
    return rng.normal(0.0, 0.1, int(settings.SAMPLE_RATE * segundos)).astype(np.float32)


def test_buffer_vacio_no_llega_al_modelo() -> None:
    settings = Settings()
    modelo = FakeModel()

    resultado = transcribe_buffer(modelo, np.zeros(0, dtype=np.float32), settings, "test")

    assert resultado.dropped
    assert resultado.drop_reason == "empty"
    assert resultado.text == ""
    assert modelo.kwargs == {}, "no hay que llamar al modelo con un buffer vacío"


def test_el_silencio_se_descarta_por_rms() -> None:
    settings = Settings()
    silencio = np.zeros(settings.SAMPLE_RATE // 2, dtype=np.float32)

    resultado = transcribe_buffer(FakeModel(), silencio, settings)

    assert resultado.drop_reason == "rms"
    assert resultado.text == ""


def test_los_segmentos_conservados_se_unen() -> None:
    settings = Settings()
    modelo = FakeModel(FakeSegment(" hola,"), FakeSegment(" buenos dias "))

    resultado = transcribe_buffer(modelo, audio_de_voz(settings), settings, "test")

    assert resultado.text == "hola, buenos dias"
    assert not resultado.dropped
    assert resultado.model == "test"
    assert modelo.kwargs["vad_filter"] is False
    assert modelo.kwargs["condition_on_previous_text"] is False
    assert modelo.kwargs["word_timestamps"] == settings.WORD_TIMESTAMPS


def test_un_segmento_que_no_es_voz_da_no_speech() -> None:
    settings = Settings()
    modelo = FakeModel(FakeSegment("algo", no_speech_prob=0.9))

    resultado = transcribe_buffer(modelo, audio_de_voz(settings), settings)

    assert resultado.drop_reason == "no_speech"
    assert resultado.no_speech_prob == 0.9


def test_un_segmento_con_poca_confianza_da_logprob() -> None:
    settings = Settings()
    modelo = FakeModel(FakeSegment("algo", avg_logprob=-1.5))

    resultado = transcribe_buffer(modelo, audio_de_voz(settings), settings)

    assert resultado.drop_reason == "logprob"
    assert resultado.avg_logprob == -1.5


def test_lo_descartado_no_tapa_lo_conservado() -> None:
    """Un segmento malo no borra el texto bueno del mismo buffer."""
    settings = Settings()
    modelo = FakeModel(
        FakeSegment("ruido", no_speech_prob=0.9),
        FakeSegment("hola"),
    )

    resultado = transcribe_buffer(modelo, audio_de_voz(settings), settings)

    assert resultado.text == "hola"
    assert not resultado.dropped
    assert resultado.no_speech_prob == 0.9, "el peor no_speech queda para el diagnóstico"


def test_la_barrera_de_contenido_descarta_la_tos() -> None:
    settings = Settings()
    modelo = FakeModel(FakeSegment(" Cof Cof "))

    resultado = transcribe_buffer(modelo, audio_de_voz(settings), settings)

    assert resultado.drop_reason == "non_speech"
    assert resultado.text == ""


def test_los_timestamps_por_palabra_salen_relativos_al_buffer() -> None:
    settings = replace(Settings(), WORD_TIMESTAMPS=True)
    palabras = [FakeWord(" hola", 0.25, 0.60), FakeWord(" buenos", 0.60, 0.90)]
    modelo = FakeModel(FakeSegment(" hola buenos ", words=palabras))

    resultado = transcribe_buffer(modelo, audio_de_voz(settings), settings)

    assert [(w.word, w.start_ms, w.end_ms) for w in resultado.words] == [
        ("hola", 0, 350),
        ("buenos", 350, 650),
    ]
    assert resultado.to_payload("final")["words"] == [
        {"w": "hola", "s": 0, "e": 350},
        {"w": "buenos", "s": 350, "e": 650},
    ]
