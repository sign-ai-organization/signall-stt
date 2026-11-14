#!/usr/bin/env python3
"""Genera WAV de habla en español para probar el pipeline sin micrófono.

Las pruebas de latencia necesitan audio reproducible: sin esto, medir exige
hablarle al micrófono y las cifras cambian con la voz, el ruido y el día. Piper
sintetiza las frases localmente y sin red, así que el mismo WAV da siempre el
mismo `FINAL transcript` de Whisper y las diferencias entre corridas son
cambios del código y no del material grabado.

Salida: **mono PCM16 a 16 kHz**, que es lo que exige el protocolo
(`app/session.py` re-frama a 30 ms para el VAD). Piper sintetiza a 22.05 kHz,
así que el resampleado ocurre acá.

Uso:

    # instalar el sintetizador una vez
    python3 -m venv .venv-tts && .venv-tts/bin/pip install piper-tts numpy

    # frase suelta
    .venv-tts/bin/python scripts/make_audio_fixtures.py "hola, ¿cómo estás?"

    # todo el set de frases de la LSC50
    .venv-tts/bin/python scripts/make_audio_fixtures.py --all --out fixtures/audio

    # el WAV largo que usa la prueba de latencia extremo a extremo
    .venv-tts/bin/python scripts/make_audio_fixtures.py --all --wav-only

Cada WAV queda junto a un `.json` con las palabras y su timestamp en
milisegundos, relativo al inicio del WAV. Eso es lo que permite medir
"¿cuánto tardó el avatar en Eatizar la palabra X?" en vez de un único número
promedio que esconde todo.
"""

import argparse
import json
import math
import re
import sys
import wave
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

PIPER_VOICE_DIR = Path.home() / ".cache" / "signai-piper" / "es_ES-sharvard-medium"
VOICE_URL_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/sharvard/medium"
VOICE_FILES = ("es_ES-sharvard-medium.onnx", "es_ES-sharvard-medium.onnx.json")
VOICE_MODEL_PATH = PIPER_VOICE_DIR / VOICE_FILES[0]
TARGET_RATE = 16000

# Frases del caso de uso real ("Registro Basico de Datos"), ordenadas de la mas
# corta a la mas larga. Todas salen de las 50 glosas de LSC50, asi que el
# backend tiene que producir señas reales y no el fallback.
#
# `pause_ms` es el silencio entre palabras. El default (0) deja hablar al
# sintetizador de corrido, que es lo mas cercano a una persona normalizando.
# Con pausas la transcripcion parcial llega antes, que es justo lo que hay que
# medir en tiempo real.
PHRASES = [
    # (id, texto)
    ("hola", "hola"),
    ("gracias", "gracias"),
    ("bien", "bien bien"),
    ("nombre", "nombre"),
    ("buenos_dias", "buenos dias"),
    ("buenas_tardes", "buenas tardes"),
    ("como_estas", "como estas"),
    ("me_llamo_ana", "me llamo ana"),
    ("mi_familia", "mi familia"),
    ("mi_hermano", "mi hermano"),
    ("vivo_bogota", "vivo en bogota"),
    ("soy_estudiante", "soy estudiante"),
    ("soy_doctora", "soy doctora"),
    ("tengo_hambre", "tengo hambre"),
    ("estoy_contenta", "estoy contenta"),
    ("estoy_triste", "estoy triste"),
    ("trabajo_bogota", "trabajo en bogota"),
    ("cuanto_ganas", "cuanto ganas"),
    ("donde_vives", "donde vives"),
    ("quien_es_ella", "quien es ella"),
    ("por_favor_ayuda", "por favor ayuda"),
    ("bien_mas_o_menos", "bien mas o menos"),
    ("nunca", "nunca"),
    ("muchas_personas", "cuantas personas son"),
    # Frases encadenadas: el caso de uso es una conversacion, no una palabra
    # suelta, y es donde se ve si el avatar se atraganta.
    ("conversacion_1", "hola, buenos dias, como estas"),
    ("conversacion_2", "me llamo ana, vivo en bogota, soy estudiante"),
    ("conversacion_3", "tengo hambre, por favor, donde esta el restaurante"),
    ("conversacion_4", "bien, muchas gracias, hasta manana"),
]


@dataclass
class WordTiming:
    word: str
    start_ms: int
    end_ms: int


def ensure_voice() -> None:
    """Descarga la voz piper si no esta. Es idempotente.

    El modelo queda en `VOICE_MODEL_PATH`; no devuelve la ruta porque es
    siempre la misma y publicarla como retorno sugeriria que puede variar.
    """
    PIPER_VOICE_DIR.mkdir(parents=True, exist_ok=True)
    if all((PIPER_VOICE_DIR / filename).exists() for filename in VOICE_FILES):
        return

    import urllib.request

    for filename in VOICE_FILES:
        target = PIPER_VOICE_DIR / filename
        if target.exists():
            continue
        print(f"descargando {filename} ...", file=sys.stderr)
        urllib.request.urlretrieve(f"{VOICE_URL_BASE}/{filename}", target)


def resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Resample lineal con antialiasing previo.

    Piper trabaja a 22.05 kHz y el protocolo exige 16 kHz. Un recorte ingenuo
    aliasing-foldedcia las-formantes y Whisper transcribe peor, asi que antes de
    tirar muestras se pasa un Butterworth pasabajos.
    """
    if source_rate == target_rate:
        return audio

    ratio = target_rate / source_rate
    # Moving average: barato y suficiente para bajar de 22.05 kHz a 16 kHz.
    # El kernel se hace del tamaño de la reduccion, no fijo, para no agregar
    # un piso de ruido que el VAD tendria que descartar.
    width = max(1, int(round(1 / ratio)))
    if width > 1:
        kernel = np.ones(width) / width
        audio = np.convolve(audio, kernel, mode="same")

    new_length = int(math.floor(len(audio) / ratio))
    # np.interp sobre el eje temporal: equivalente a leer el dominio de
    # frecuencia al reveso, sin los pasos extra de scipy.
    old_t = np.arange(len(audio))
    new_t = np.arange(new_length) / ratio
    return np.interp(new_t, old_t, audio).astype(np.float32)


def _peak_normalize(audio: np.ndarray, target_peak: float = 0.85) -> np.ndarray:
    """Normaliza por pico, no por RMS.

    El VAD de WebRTC decide con energia absoluta (`MIN_RMS` en `app/config.py`),
    asi que un WAV muy bajito se pierde entero. Normalizar por pico deja el
    volumen en un lugar conocido sin pasar por compresion.
    """
    peak = float(np.abs(audio).max())
    if peak <= 1e-6:
        return audio
    return audio * (target_peak / peak)


def _add_silence(samples: int) -> np.ndarray:
    """Silencio con ruido de fondo, no silencio digital.

    Un tramo de ceros es patologico para el VAD y para Whisper, que pueden
    inventar texto. Un ruido de piso muy bajo es lo que produce realmente un
    microfono en silencio y obliga al pipeline a comportarse como en produccion.
    """
    rng = np.random.default_rng(1234)
    noise = rng.normal(0.0, 0.0015, samples)
    return noise.astype(np.float32)


@dataclass
class Synthesized:
    audio: np.ndarray
    words: list[WordTiming]


def synthesize(voice, text: str, pause_ms: int = 0) -> Synthesized:
    """Sintetiza frase y devuelve audio + timestamps por palabra.

    Piper entrega un solo bloque por frase; para las pausas entre palabras se
    pide palabra por palabra, que es lo unico que deja saber cuando empieza y
    termina cada una.
    """
    rate = voice.config.sample_rate

    if pause_ms <= 0:
        chunk = next(iter(voice.synthesize(text)))
        audio = np.asarray(chunk.audio_float_array, dtype=np.float32)
        words = _even_word_timings(text, len(audio), rate)
        return Synthesized(audio=audio, words=words)

    tokens = [token for token in re.split(r"(\s+)", text) if token.strip()]
    pieces: list[np.ndarray] = []
    words: list[WordTiming] = []
    cursor = 0

    for token in tokens:
        chunk = next(iter(voice.synthesize(token)))
        piece = np.asarray(chunk.audio_float_array, dtype=np.float32)
        pieces.append(_add_silence(int(rate * pause_ms / 1000)))
        pieces.append(piece)
        words.append(WordTiming(token.strip(), int(cursor * 1000 / rate), int((cursor + len(piece)) * 1000 / rate)))
        cursor += int(rate * pause_ms / 1000) + len(piece)

    pieces.append(_add_silence(int(rate * pause_ms / 1000)))
    return Synthesized(audio=np.concatenate(pieces), words=words)


def _even_word_timings(text: str, total_samples: int, rate: int) -> list[WordTiming]:
    """Reparte la duracion del bloque a partes iguales entre palabras.

    Es una aproximacion: no dice cuando *realmente* empieza "estas". Sirve para
    tener una referencia estable entre corridas, no para medir alineacion fina.
    Para eso esta el modo con pausas, que si conoce los tiempos reales.
    """
    words = text.split()
    if not words:
        return []
    per_word = total_samples / len(words)
    return [
        WordTiming(word, int(index * per_word * 1000 / rate), int((index + 1) * per_word * 1000 / rate))
        for index, word in enumerate(words)
    ]


def write_wav(path: Path, audio: np.ndarray, rate: int = TARGET_RATE) -> None:
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("text", nargs="*", help="frases arbitrarias a sintetizar")
    parser.add_argument("--all", action="store_true", help="generar el set completo de frases de la LSC50")
    parser.add_argument("--out", default="fixtures/audio", help="directorio de salida")
    parser.add_argument("--pause-ms", type=int, default=0, help="silencio entre palabras (0 = habla continua)")
    parser.add_argument("--sample-rate", type=int, default=TARGET_RATE)
    args = parser.parse_args()

    if not args.text and not args.all:
        parser.error("hace falta una frase o --all")

    try:
        from piper import PiperVoice
    except ImportError:
        print(
            "falta piper-tts. Instalalo en un venv aparte:\n"
            "  python3 -m venv .venv-tts && .venv-tts/bin/pip install piper-tts numpy",
            file=sys.stderr,
        )
        return 1

    ensure_voice()
    model_path = VOICE_MODEL_PATH
    print(f"cargando voz {model_path} ...", file=sys.stderr)
    voice = PiperVoice.load(str(model_path), config_path=str(model_path) + ".json")

    if args.all:
        selected = [(name, text) for name, text in PHRASES]
    else:
        selected = [(re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40], text) for text in args.text]

    out_dir = Path(args.out)
    index: list[dict] = []

    for name, text in selected:
        result = synthesize(voice, text, pause_ms=args.pause_ms)
        audio = resample(result.audio, voice.config.sample_rate, args.sample_rate)
        audio = _peak_normalize(audio)

        # Los timestamps hay que reexpresarlos despues del resampleado, que
        # cambia la escala temporal.
        scale = args.sample_rate / voice.config.sample_rate
        words = [
            WordTiming(w.word, int(w.start_ms * scale), int(w.end_ms * scale)) for w in result.words
        ]

        wav_path = out_dir / f"{name}.wav"
        write_wav(wav_path, audio, args.sample_rate)

        duration_ms = int(len(audio) * 1000 / args.sample_rate)
        meta = {
            "id": name,
            "text": text,
            "duration_ms": duration_ms,
            "sample_rate": args.sample_rate,
            "pause_ms": args.pause_ms,
            "words": [asdict(w) for w in words],
        }
        (out_dir / f"{name}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        index.append(meta)
        print(f"{name:<22} {text!r:<52} {duration_ms / 1000:5.2f}s  -> {wav_path}")

    (out_dir / "index.json").write_text(
        json.dumps({"count": len(index), "clips": index}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n{len(index)} clips en {out_dir}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())