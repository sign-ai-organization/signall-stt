"""Pruebas del planificador de inferencia con trabajos de mentira.

No hay GPU ni modelo: lo que se prueba es el contrato de `InferenceScheduler`:
el resultado y el error de la inferencia vuelven a quien pidió, y el servicio
puede arrancarlo y pararlo sin dejar tareas colgadas.
"""

import asyncio
import threading
import time

import pytest

from app.transcriber import PRIORITY_FINAL, InferenceScheduler


def _modelo_roto() -> None:
    raise ValueError("modelo roto")


def test_el_resultado_vuelve_a_quien_pidio() -> None:
    async def escenario():
        scheduler = InferenceScheduler(1)
        try:
            return await scheduler.run(PRIORITY_FINAL, sum, [1, 2, 3])
        finally:
            await scheduler.stop()

    # `run` arranca el planificador solo, sin un `start()` explícito.
    assert asyncio.run(escenario()) == 6


def test_el_error_de_la_inferencia_llega_a_quien_pidio() -> None:
    async def escenario():
        scheduler = InferenceScheduler(1)
        try:
            await scheduler.run(PRIORITY_FINAL, _modelo_roto)
        finally:
            await scheduler.stop()

    with pytest.raises(ValueError, match="modelo roto"):
        asyncio.run(escenario())


def test_parar_el_planificador_lo_deja_reutilizable() -> None:
    async def escenario():
        scheduler = InferenceScheduler(1)
        await scheduler.start()
        await scheduler.stop()
        await scheduler.start()
        try:
            return await scheduler.run(PRIORITY_FINAL, len, [1, 2])
        finally:
            await scheduler.stop()

    assert asyncio.run(escenario()) == 2


def test_una_inferencia_en_vuelo_se_cancela_al_parar() -> None:
    """Parar el servicio no deja al solicitante esperando una respuesta.

    El worker se cancela a mitad de una inferencia; quien pidió tiene que
    enterarse como cancelación y no quedarse colgado del futuro.
    """

    async def escenario():
        scheduler = InferenceScheduler(1)
        arrancó = threading.Event()

        def inferencia_larga() -> None:
            arrancó.set()
            time.sleep(0.05)

        tarea = asyncio.create_task(scheduler.run(PRIORITY_FINAL, inferencia_larga))
        await asyncio.to_thread(arrancó.wait, 5)
        await scheduler.stop()
        return tarea

    tarea = asyncio.run(escenario())

    with pytest.raises(asyncio.CancelledError):
        tarea.result()
