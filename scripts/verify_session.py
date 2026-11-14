#!/usr/bin/env python3
"""Pruebas de la lógica de `app/session.py`: buffering, VAD, cierre de frase.

Por qué existen
---------------

`SILENCE_TIMEOUT_MS` es el mayor término de la latencia de punta a punta (1500 ms
de los 4011 ms de una corrida del 16 de octubre) y es un número que se quiere
bajar, pero bajarlo parte las frases donde la persona respira. Con el archivo sin
una sola prueba, cualquier cambio acá era a ciegas: nadie podía decir si había
roto algo o no.

Por qué no usan el modelo ni el VAD reales
------------------------------------------

Whisper y WebRTC VAD son de terceros y son lentos. Lo que hay que probar es la
lógica de este repo —cuándo se cierra una frase, qué audio entra al buffer, dónde
se parte una frase larga—, y esa lógica se puede ejercitar con un VAD que decide
por amplitud y un planificador que devuelve texto fijo. Consequences:

  - Las pruebas corren en milisegundos y sin GPU, así que se pueden poner en un
    bucle de regresión sin pedirle nada a nadie.
  - No dependen de `fixtures/audio/*.wav`, que **no están en git** (315 MB), ni
    de que Piper esté instalado.

Lo que sí es real en cada prueba: los frames de 30 ms de PCM16, `Settings` con sus
defaults, y el `StreamingSession` entero.

Uso
---

    python -m scripts.verify_session
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.session import StreamingSession  # noqa: E402
from app.transcriber import (  # noqa: E402
    PRIORITY_FINAL,
    PRIORITY_PARTIAL,
    TranscriptionResult,
    transcribe_buffer,
)


# ── Dobles de prueba ───────────────────────────────────────────────────────────


class FakeVad:
    """VAD que decide por amplitud: voz si el frame supera [umbral].

    El VAD real (WebRTC) es un modelo; lo que importa acá es qué hace la sesión
    con su veredicto, no con qué criterio lo dio.
    """

    def __init__(self, umbral: float = 0.02) -> None:
        self.umbral = umbral

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        samples = np.frombuffer(frame, dtype=np.int16)
        # WebRTC VAD solo acepta frames de 10, 20 o 30 ms; el doble rechaza un
        # frame mal armado por la misma razón que el real fallaría.
        if len(samples) * 1000 / sample_rate not in (10, 20, 30):
            return False
        rms = float(np.sqrt(np.mean((samples.astype(np.float32) / 32768.0) ** 2)))
        return rms >= self.umbral


class FakeScheduler:
    """Planificador que no ejecuta nada: devuelve siempre el mismo texto.

    Registra la prioridad de cada pedido y cuántos milisegundos de audio recibió,
    que es lo que permite afirmar tanto *qué* se transcribió como *cuánto*.
    """

    def __init__(self, texto: str = "hola", sample_rate: int = 16000) -> None:
        self.texto = texto
        self._sample_rate = sample_rate
        self.prioridades: list[int] = []
        self.audios_ms: list[float] = []

    async def run(self, priority: int, func, *args) -> TranscriptionResult:
        # La sesión llama `run(prioridad, transcribe_buffer, modelo, audio,
        # settings, model_size)`: el audio es el segundo argumento posicional.
        # `func` no se ejecuta —no hay modelo— pero se comprueba que la sesión
        # despache la función real y no otra cosa.
        assert func is transcribe_buffer, f"la sesión despachó {func!r}"
        audio = args[1]
        audio_ms = len(audio) / self._sample_rate * 1000
        self.prioridades.append(priority)
        self.audios_ms.append(audio_ms)
        return TranscriptionResult(
            text=self.texto,
            audio_ms=audio_ms,
            elapsed_ms=1.0,
            model="fake",
        )


def sesion(
    texto: str = "hola",
    vad: FakeVad | None = None,
    **ajustes: object,
) -> tuple[StreamingSession, FakeScheduler]:
    """Una sesión con el VAD y el planificador falsos, todo lo demás real.

    [ajustes] pisa campos de `Settings` para probar una configuración que no es
    la del servicio (por ejemplo, un `PARTIAL_INTERVAL_MS` de 1 ms, porque el
    intervalo se mide contra el reloj de pared y las pruebas corren en
    microsegundos).
    """
    settings = replace(Settings(), **ajustes) if ajustes else Settings()
    scheduler = FakeScheduler(texto, settings.SAMPLE_RATE)
    session = StreamingSession(model=None, scheduler=scheduler, vad=vad or FakeVad(), settings=settings)
    return session, scheduler


def finales(eventos: list[dict]) -> list[dict]:
    """Solo los finales: un `partial` no es una frase cerrada."""
    return [e for e in eventos if e["type"] == "final"]


# ── Audio de prueba ────────────────────────────────────────────────────────────


def frames_de(milisegundos: int, settings: Settings) -> int:
    """Cuántos frames de `FRAME_DURATION_MS` entran en [milisegundos]."""
    return max(1, milisegundos // settings.FRAME_DURATION_MS)


def frame_voz(settings: Settings) -> bytes:
    """30 ms de ruido con amplitud claramente de voz."""
    rng = np.random.default_rng(7)
    samples = (rng.normal(0.0, 0.3, settings.frame_size) * 32768).astype(np.int16)
    return samples.tobytes()


def frame_silencio(settings: Settings) -> bytes:
    """30 ms de silencio digital, que es como llega la pausa de la persona."""
    return bytes(settings.frame_size * 2)


async def alimentar(session: StreamingSession, frame: bytes, veces: int) -> list[dict]:
    """Manda [veces] frames de [frame] de a uno, y junta lo que salió."""
    eventos: list[dict] = []
    for _ in range(veces):
        eventos.extend(await session.feed(frame))
    return eventos


# ── Pruebas ────────────────────────────────────────────────────────────────────


async def test_ventana_de_inicio_no_transcribe_al_vuelo() -> None:
    """Un arranque de voz no dispara una transcripción antes de tiempo.

    La ventana de `SPEECH_ONSET_WINDOW_MS` existe para no empezar a transcribir el
    ruido que precede a la frase. Si no estuviera, el primer frame de ruido ya
    llegaría al modelo.
    """
    settings = Settings()
    session, scheduler = sesion()
    ventana = frames_de(settings.SPEECH_ONSET_WINDOW_MS, settings)

    for _ in range(ventana - 1):
        await session.feed(frame_voz(settings))
    assert scheduler.prioridades == [], f"transcribió antes de llenar la ventana: {scheduler.prioridades}"
    assert not session._is_speaking, "el utterance arrancó antes de llenar la ventana"

    # El frame que completa la ventana sí debe haber arrancado el utterance.
    eventos = await session.feed(frame_voz(settings))
    assert session._is_speaking, "la ventana se llenó y el utterance no arrancó"
    assert finales(eventos) == [], f"la ventana no debería cerrar la frase: {eventos}"


async def test_el_silencio_cierra_la_frase() -> None:
    """`SILENCE_TIMEOUT_MS` de silencio después de hablar produce un final.

    Es el número que se quiere bajar, así que tiene que estar medido por una
    prueba y no de memoria.
    """
    settings = Settings()
    session, scheduler = sesion("buenos dias")
    umbral = frames_de(settings.SILENCE_TIMEOUT_MS, settings)

    await alimentar(session, frame_voz(settings), 30)
    eventos = await alimentar(session, frame_silencio(settings), umbral - 1)
    assert finales(eventos) == [], f"cerró con {settings.SILENCE_TIMEOUT_MS - settings.FRAME_DURATION_MS} ms de silencio"

    eventos = await session.feed(frame_silencio(settings))
    cerrados = finales(eventos)
    assert len(cerrados) == 1, f"esperaba 1 final, hubo {len(cerrados)}"
    assert cerrados[0]["text"] == "buenos dias"
    assert scheduler.prioridades[-1] == PRIORITY_FINAL, "el final no se transcribió con prioridad de final"


async def test_un_silencio_mas_corto_no_parte_la_frase() -> None:
    """La pausa de una respiración no cierra la frase.

    Es la razón documentada para no bajar `SILENCE_TIMEOUT_MS`: 300 ms de pausa
    son una respiración normal y tienen que quedar dentro de la misma frase.
    """
    settings = Settings()
    session, scheduler = sesion()

    for _ in range(3):
        await alimentar(session, frame_voz(settings), 20)
        eventos = await alimentar(session, frame_silencio(settings), 10)   # 300 ms
        assert finales(eventos) == [], f"una pausa de 300 ms partió la frase: {eventos}"

    # Al final, el cierre por el silencio largo sí tiene que cerrar.
    umbral = frames_de(settings.SILENCE_TIMEOUT_MS, settings)
    eventos = await alimentar(session, frame_silencio(settings), umbral)
    assert finales(eventos), "la pausa larga no cerró la frase"


async def test_el_silencio_final_se_recorta() -> None:
    """Al buffer no entra el silencio entero, solo el que se conserva.

    `FINAL_TRAILING_SILENCE_KEEP_MS` deja 300 ms de cola: lo justo para que la
    última consonante no se coma el corte, sin pagarle al modelo 1200 ms de
    silencio digital.
    """
    settings = Settings()
    session, scheduler = sesion()
    await alimentar(session, frame_voz(settings), 30)
    await alimentar(session, frame_silencio(settings), 60)   # 1800 ms de pausa

    assert scheduler.audios_ms, "nunca se transcribió"
    enviados = scheduler.audios_ms[-1]
    esperado_ms = 30 * settings.FRAME_DURATION_MS + settings.FINAL_TRAILING_SILENCE_KEEP_MS
    # Un frame de tolerancia: el frame que dispara el cierre ya entró al buffer.
    assert abs(enviados - esperado_ms) <= settings.FRAME_DURATION_MS, (
        f"mandó {enviados:.0f} ms de audio y se esperaban {esperado_ms} ms"
    )


async def test_flush_responde_siempre_incluso_vacio() -> None:
    """`flush()` contesta aunque no haya nada, y `feed()` se calla.

    Es lo que hace que soltar el micrófono sin hablar devuelva una respuesta en
    vez de dejar al cliente esperando su propio timeout.
    """
    settings = Settings()
    session, _ = sesion()

    vacio = await session.flush()
    assert vacio["type"] == "final", f"flush vacío devolvió {vacio}"
    assert vacio["text"] == "", "un flush sin audio no puede devolver texto"

    # El camino por silencio no manda vacíos: ahí nadie preguntó.
    session2, _ = sesion()
    await alimentar(session2, frame_silencio(settings), 60)
    eventos = await alimentar(session2, frame_silencio(settings), 60)
    assert finales(eventos) == [], "mandó un final vacío sin que nadie preguntara"


async def test_el_final_deja_lista_la_siguiente_frase() -> None:
    """Después de un final, la sesión vuelve a arrancar en cero.

    Si el estado no se limpia, la segunda frase hereda el silencio acumulado de la
    primera y se cierra sola antes de que nadie hable.
    """
    settings = Settings()
    session, scheduler = sesion()

    umbral = frames_de(settings.SILENCE_TIMEOUT_MS, settings)
    await alimentar(session, frame_voz(settings), 20)
    await alimentar(session, frame_silencio(settings), umbral)

    assert not session._is_speaking, "siguió creyendo que estaba hablando"
    assert session._silence_ms == 0, f"conservó {session._silence_ms} ms de silencio"

    eventos = await alimentar(session, frame_voz(settings), 20)
    assert finales(eventos) == [], "la segunda frase se cerró sola"


async def test_el_tope_del_buffer_corta_la_frase() -> None:
    """Al llegar a `MAX_BUFFER_MS` la frase se corta aunque nadie se calle.

    Sin este tope, alguien que habla sin pausas acumularía audio para siempre y el
    buffer crecería sin límite. El corte cae en un borde de frase, no en medio de
    una palabra, y por eso el audio que se manda es un poco menos que el tope.
    """
    settings = Settings()
    session, scheduler = sesion()
    maximo = frames_de(settings.MAX_BUFFER_MS, settings)

    eventos: list[dict] = []
    for _ in range(maximo + 2):
        eventos = await session.feed(frame_voz(settings))
        if finales(eventos):
            break

    cortados = finales(eventos)
    assert cortados, f"llegó a {settings.MAX_BUFFER_MS} ms y no cortó nada"
    assert scheduler.audios_ms[-1] > 100 * settings.FRAME_DURATION_MS, (
        f"cortó en {scheduler.audios_ms[-1]:.0f} ms, demasiado temprano"
    )
    # El corte cae en el frame que cruzó el tope, así que puede pasarse por uno.
    assert scheduler.audios_ms[-1] <= settings.MAX_BUFFER_MS + settings.FRAME_DURATION_MS, (
        f"mandó {scheduler.audios_ms[-1]:.0f} ms, más que el tope del buffer"
    )


async def test_el_corte_cae_en_la_pausa() -> None:
    """Cuando hay una pausa, el corte del tope cae ahí y no en mitad de palabra.

    Es la diferencia entre partir la frase donde la persona respiró —que es un
    corte natural— y partirla donde toque. Sin esto, cualquier frase larga se
    cortaría por el medio de una palabra.
    """
    settings = Settings()
    session, scheduler = sesion()

    await alimentar(session, frame_voz(settings), frames_de(4000, settings))
    await alimentar(session, frame_silencio(settings), frames_de(500, settings))
    maximo = frames_de(settings.MAX_BUFFER_MS, settings)
    for _ in range(maximo):
        eventos = await session.feed(frame_voz(settings))
        if finales(eventos):
            break

    assert finales(eventos), "una frase con pausa llegó al tope y no se cortó"
    enviados = scheduler.audios_ms[-1]
    # El corte quedó en la pausa (unos 4,2 s), no en el tope de 8 s: si hubiera
    # salido al tope, la segunda mitad de la frase se habría cortado por el medio.
    assert 3500 <= enviados <= 5000, f"cortó en {enviados:.0f} ms, y la pausa estaba cerca de 4000"


async def test_el_parcial_se_pide_con_prioridad_de_parcial() -> None:
    """El parcial se lanza con su propia prioridad y no con la del final.

    Es lo que garantiza que un final recién llegado no quede esperando detrás de
    un parcial en vuelo.
    """
    settings = Settings()
    session, scheduler = sesion("parcial", PARTIAL_INTERVAL_MS=1)
    # `PARTIAL_INTERVAL_MS` necesita audio suficiente y el intervalo cumplido.
    for _ in range(frames_de(600, settings)):
        await session.feed(frame_voz(settings))
        # El intervalo entre parciales se mide contra el reloj de pared, y las
        # pruebas corren en microsegundos: hay que dejarlo pasar.
        await asyncio.sleep(0.003)
    if session._partial_task is not None:
        await asyncio.gather(session._partial_task, return_exceptions=True)
    assert PRIORITY_PARTIAL in scheduler.prioridades, (
        f"ningún parcial se lanzó; prioridades vistas: {scheduler.prioridades}"
    )


async def test_audio_ms_del_final() -> None:
    """`audio_ms` viaja siempre: el cliente lo usa para distinguir frases."""
    settings = Settings()
    session, _ = sesion("hola que tal")
    umbral = frames_de(settings.SILENCE_TIMEOUT_MS, settings)
    await alimentar(session, frame_voz(settings), 25)
    eventos = await alimentar(session, frame_silencio(settings), umbral)
    final = next(e for e in eventos if e["type"] == "final")
    esperado = 25 * settings.FRAME_DURATION_MS + settings.FINAL_TRAILING_SILENCE_KEEP_MS
    assert abs(final["audio_ms"] - esperado) <= settings.FRAME_DURATION_MS * 2, (
        f"audio_ms={final['audio_ms']}, se esperaba ~{esperado}"
    )


# ── Corredor ───────────────────────────────────────────────────────────────────

PRUEBAS = [
    test_ventana_de_inicio_no_transcribe_al_vuelo,
    test_el_silencio_cierra_la_frase,
    test_un_silencio_mas_corto_no_parte_la_frase,
    test_el_silencio_final_se_recorta,
    test_flush_responde_siempre_incluso_vacio,
    test_el_final_deja_lista_la_siguiente_frase,
    test_el_tope_del_buffer_corta_la_frase,
    test_el_corte_cae_en_la_pausa,
    test_el_parcial_se_pide_con_prioridad_de_parcial,
    test_audio_ms_del_final,
]


async def main() -> int:
    print("=== session.py: buffering, VAD y cierre de frase ===\n")
    fallos = 0
    for prueba in PRUEBAS:
        nombre = prueba.__name__.removeprefix("test_").replace("_", " ")
        try:
            await prueba()
            print(f"  PASS  {nombre}")
        except AssertionError as exc:
            fallos += 1
            print(f"  FAIL  {nombre}\n        {exc}")
        except Exception as exc:  # noqa: BLE001 - una excepción también es un fallo
            fallos += 1
            print(f"  ERROR {nombre}\n        {type(exc).__name__}: {exc}")

    print()
    if fallos:
        print(f"{fallos} de {len(PRUEBAS)} pruebas fallaron")
        return 1
    print(f"todo OK ({len(PRUEBAS)} pruebas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))