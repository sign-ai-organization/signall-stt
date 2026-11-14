# SonarQube en cero y cobertura conectada

- **Fecha:** 2026-11-14
- **Rama:** `main`
- **Origen:** pedido del owner dentro de la reestructuración: dejar el proyecto
  `signall-stt` del SonarQube local sin issues y con cobertura medida de verdad.

---

## Qué llegó

El SonarQube local reportaba **5 issues** en `signall-stt` y **0 % de cobertura**:

| Archivo | Regla | Qué decía |
|---|---|---|
| `app/transcriber.py:243` | `python:S3776` | complejidad cognitiva 19 (máximo 15) en `transcribe_buffer` |
| `app/transcriber.py:221` | `python:S5754` | `SystemExit` no se re-lanzaba: se atrapaba `BaseException` entero |
| `scripts/verify_session.py:64` | `python:S1172` | parámetro `sample_rate` sin usar en `FakeVad.is_speech` |
| `scripts/verify_session.py:83` | `python:S1172` | parámetro `func` sin usar en `FakeScheduler.run` |
| `scripts/make_audio_fixtures.py:100` | `python:S3516` | `ensure_voice` devolvía siempre el mismo valor |

La cobertura era 0 % porque no había reporte: `sonar.python.coverage.reportPaths` estaba
comentado y no existía ni un test que generara `coverage.xml`.

## Qué se hizo

Sin cambiar el comportamiento. Los dos scripts de verificación se corrieron antes y
después de cada cambio.

### `app/transcriber.py`

- El recorrido de los segmentos del modelo se movió a **`_collect_segments`**. Hace
  exactamente lo mismo que el `for` que estaba adentro de `transcribe_buffer`: acumula el
  texto que pasa los umbrales, las palabras si `WORD_TIMESTAMPS` está prendido, y las
  peores `avg_logprob`/`no_speech_prob` (que son el diagnóstico del descarte). La
  complejidad de `transcribe_buffer` baja de 19 a 6.
- `InferenceScheduler._worker` separó el trabajo de un pedido en **`_run_job`**, y el
  `except BaseException` se partió en dos:
  - `except asyncio.CancelledError`: le avisa al solicitante con el mismo
    `future.set_exception(...)` de antes y se **re-lanza**, para que el worker termine.
  - `except Exception`: es el error de inferencia y se entrega al solicitante como antes.
  - `SystemExit` y `KeyboardInterrupt` ya no se atrapan: son del proceso y tienen que
    propagarse. Es lo que pedía `S5754` y no cambia nada del camino de una transcripción
    (ninguno de los dos aparece en una inferencia).

### `scripts/verify_session.py`

- `FakeVad.is_speech` usa `sample_rate` para rechazar frames que no sean de 10, 20 o 30 ms,
  igual que el VAD real de WebRTC. Los tests mandan frames de 30 ms, así que el veredicto
  no cambia.
- `FakeScheduler.run` usa `func` para comprobar que la sesión despacha
  `transcribe_buffer` y no otra cosa. El doble sigue sin ejecutarla.

### `scripts/make_audio_fixtures.py`

- `ensure_voice()` ya no devuelve la ruta: descarga si falta y listo. La ruta es un
  constante del módulo, `VOICE_MODEL_PATH`, porque siempre es la misma.

### Cobertura

- `requirements-dev.txt`: se agregaron `pytest==9.*`, `pytest-cov==7.*` y `coverage==7.*`.
- `.coveragerc`: se mide `app/` con ramas y **rutas relativas**. Sin
  `relative_files = True`, `coverage.xml` publica rutas absolutas del contenedor y
  SonarQube no engancha ningún archivo: la cobertura vuelve a 0 %.
- `tests/`: los dos scripts de verificación envueltos en pytest (`test_verify_scripts.py`),
  más `test_transcriber.py` (con un modelo falso: vacío, RMS, `no_speech`, `logprob`,
  barrera de contenido y timestamps), `test_scheduler.py` (resultado, error y cancelación
  del pedido en vuelo), `test_metrics.py` (histogramas, percentiles, la muestra circular,
  el render de Prometheus y `ConcurrencyGauge`) y `test_api.py` (los cuatro endpoints y el
  WebSocket con `TestClient`, modelo y planificador falsos). `conftest.py` deja la raíz en
  el `sys.path`.
- **`scripts/cobertura.sh`**. Corre los dos scripts y `pytest tests/` bajo
  `coverage run --parallel-mode`, hace `coverage combine` y escribe `coverage.xml` en la
  raíz. Corre en un contenedor descartable de la imagen del servicio
  —es la que tiene `numpy`, `webrtcvad` y `faster-whisper`— e instala ahí mismo
  `requirements-dev.txt`. Los artefactos se devuelven al usuario del host y `pytest` no
  deja caché, para que la segunda corrida no falle por permisos.
- `sonar-project.properties`: `sonar.python.coverage.reportPaths=coverage.xml`,
  `sonar.tests=tests` y `sonar.coverage.exclusions=scripts/**` (los scripts son el
  instrumento de medida, no código del servicio).

### `scripts/verify_non_speech.py`

Las comprobaciones pasaron de estar en el cuerpo del módulo a `main()`, con
`if __name__ == "__main__"`. La salida y el código de salida son los mismos; lo que
cambia es que ahora se puede importar desde pytest sin ejecutar todo al importar.

## Qué se midió

Antes y después, contra el SonarQube local
(`api/issues/search?componentKeys=signall-stt`, `api/measures/component?component=signall-stt`),
el 2026-11-14 a las 07:25 (antes) y 07:36 UTC (después de la segunda vuelta, ver abajo).

| | Antes | Después |
|---|---|---|
| Issues abiertos | 5 | **0** |
| Cobertura del proyecto | 0,0 % | **90,0 %** |
| Cobertura de líneas | 0,0 % | 90,9 % |
| Cobertura de ramas | 0,0 % | 85,5 % |
| Pruebas | 0 | 54 |

Por archivo de `app/`, según `./scripts/cobertura.sh`: `non_speech.py` 100 %,
`metrics.py` 100 %, `config.py` 96 %, `session.py` 89 %, `main.py` 85 %,
`transcriber.py` 83 %.

### Segunda vuelta: el piso del owner era 80 %

La primera corrida cerraba en 72,9 % y el owner pidió 80 % o más. Se agregaron dos
archivos de pruebas, sin tocar código de producción:

- `tests/test_metrics.py` (19 casos): percentiles, muestra circular de 512, contadores y
  gauges en mapas separados, `/metrics` en formato Prometheus (`# HELP`/`# TYPE`, buckets
  acumulativos, sufijos `_sum`/`_count`/`_min`/`_max`/`_p50`/`_p95`/`_p99`, enteros sin
  decimales), `Timer` y `ConcurrencyGauge` (incluido el piso en cero). `metrics.py` pasa de
  58 % a 100 %.
- `tests/test_api.py` (12 casos): `/health` con y sin modelo, `/metrics`, `/metrics.json`,
  `/transcribe` (camino bueno y los tres rechazos de WAV) y `/ws/transcribe` (un `end` sin
  audio, un texto que no es `end`, y una frase cerrada por silencio). `main.py` pasa de
  0 % a 85 %. `TestClient` no corre el `lifespan` si no se lo usa como context manager, así
  que Whisper nunca se carga; el estado del servicio se arma a mano con el VAD y el
  planificador falsos. `app/main.py` no cambió, y `httpx` se sumó a `requirements-dev.txt`
  porque es lo que usa `TestClient`.

## Qué quedó pendiente

- **El `lifespan` de `app/main.py` (líneas 27-36) no se cubre**: carga el modelo de Whisper,
  hace el precalentado y arranca el planificador con VAD real. Eso necesita modelo y GPU,
  no es lógica de la app. Las pruebas arman `state` a mano, que es lo mismo que queda
  después del `lifespan`.
- **`app/main.py:143-144`, el `except WebSocketDisconnect`**: el `TestClient` cierra el
  socket ordenadamente y el handler ve el mensaje `websocket.disconnect`, así que sale por
  el `break` y no por la excepción. Para forzarla habría que romper el ASGI a mano.
- **`config.py:16`**: es la rama de `_env_bool` que lee una variable de entorno; los
  `defaults` de `Settings` se evalúan al importar el módulo, así que solo se cubre
  recargándolo con el entorno cambiado.
- **`transcriber.py:108-122` y `143-149`**: `load_model` con su caída a CPU y `warm_up`.
  Modelo y GPU, igual que el `lifespan`.
- **`session.py` 139-144, 155-156, 348-354**: partes del parcial y del `flush` descartado
  que dependen del reloj de pared y del modelo real.
- **El CI no corre `pytest`**: instalar `faster-whisper` en un runner de GitHub son
  ~200 MB y hoy el workflow solo compila y busca credenciales. La cobertura se corre a
  mano con `scripts/cobertura.sh` antes de escanear Sonar.
- **La primera corrida de la cobertura construye la imagen** (`docker compose build`) e
  instala las herramientas dentro del contenedor: necesita red. Después queda en caché.
