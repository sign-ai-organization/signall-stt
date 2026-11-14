# signall-stt

Servicio de Speech-to-Text (español) para el proyecto SignAll. Recibe audio del
cliente (app móvil) y devuelve texto transcrito en tiempo real, usando
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) + WebRTC VAD.

No captura micrófono localmente: es un servicio HTTP/WebSocket que corre en
Docker. La captura de audio la hace el cliente (la app móvil, o
`scripts/mic_test_client.py` para pruebas de escritorio).

## Correr con Docker

```bash
docker compose up --build
```

El primer arranque descarga el modelo de Hugging Face (según `MODEL_SIZE`) y
lo cachea en un volumen (`hf-cache`), así que los siguientes arranques son
rápidos.

Por defecto usa GPU (`DEVICE=cuda`). Requiere NVIDIA Container Toolkit /
Docker Desktop con soporte GPU. Si no tenés GPU:

```bash
DEVICE=cpu COMPUTE_TYPE=int8 docker compose up --build
```

y quitá el bloque `deploy.resources` de `docker-compose.yml` (si no, Docker
puede fallar buscando un dispositivo GPU que no existe). Aunque quede
`DEVICE=cuda`, el servicio igual cae a CPU automáticamente si la GPU no está
disponible al cargar el modelo.

El servicio queda en `http://localhost:8000`.

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `MODEL_SIZE` | `medium` | `tiny`, `base`, `small`, `medium`, `large-v3`. Se usa para **todo**: finales y parciales |
| `DEVICE` | `cuda` | `cuda` o `cpu` |
| `COMPUTE_TYPE` | `float16` | `float16` (GPU) o `int8` (CPU) |
| `LANGUAGE` | `es` | Idioma para Whisper |
| `VAD_AGGRESSIVENESS` | `2` | 0 (permisivo) a 3 (estricto) |
| `SILENCE_TIMEOUT_MS` | `1500` | Silencio para cerrar un segmento. Es el mayor término de la latencia: bajarlo parte las frases en las pausas |
| `PARTIAL_INTERVAL_MS` | `400` | Intervalo entre parciales; `0` los desactiva |
| `MIN_AUDIO_MS_FOR_PARTIAL` | `200` | Audio mínimo antes de poder parcial |
| `SPEECH_ONSET_WINDOW_MS` | `300` | Ventana de confirmación de inicio de habla |
| `SPEECH_ONSET_RATIO` | `0.9` | Proporción mínima de frames con habla en esa ventana |
| `MAX_BUFFER_MS` | `8000` | Un segmento más largo que esto se corta en la pausa más cercana |
| `FINAL_TRAILING_SILENCE_KEEP_MS` | `300` | Silencio final que se conserva al transcribir |
| `MIN_RMS` | `0.008` | Filtro anti-alucinación por energía |
| `AVG_LOGPROB_THRESHOLD` | `-1.0` | Default de Whisper. **No subir a `-0.8`**: descarta audio que el modelo transcribió bien (medido: "cómo estás", "estoy triste") |
| `WORD_TIMESTAMPS` | `false` | Mandar `words` con inicio/fin de cada palabra. Nadie lo lee hoy; ver la nota en `app/config.py` |
| `MAX_CONCURRENT_INFERENCE` | `1` | Inferencias simultáneas en la GPU. Whisper serializa igual por abajo; con más de uno solo hay thrash |

Los seis valores de silencio e inicio de habla son los del diagrama 6.3 del
SDD. Están también explícitos en `docker-compose.yml` a propósito.

Ver `app/config.py` para la lista completa. Copiá `.env.example` a `.env` para
ajustarlas (`docker compose` lo lee solo).

## API

Este servicio **solo transcribe**. No divide en oraciones, no traduce y no
decide qué se representa: eso es del backend, que recibe la frase completa y la
manda a representar cuando el cliente se la envía como `text`. El contrato completo
está en `docs/android-integration.md` del repo hermano.

### `GET /health`

```json
{"status": "ok", "model": "medium", "device": "cuda", "language": "es", "silence_timeout_ms": 1500}
```

### `GET /metrics` y `GET /metrics.json`

Métricas en formato de exposición de Prometheus y en JSON. Las que tienen
histograma son `stt_inference_ms`, `stt_final_ms`, `stt_partial_ms`,
`stt_partial_queue_ms` (espera por la GPU), `stt_final_lag_ms`,
`stt_partial_lag_ms` (audio recibido menos el que ya estaba en el buffer cuando
arrancó la inferencia) y `stt_end_to_final_ms` (dejar de hablar → final
disponible). Los contadores incluyen `stt_dropped_total{reason}`, donde el
`reason` dice *por qué* se descartó: `rms`, `no_speech`, `logprob` o `empty`.

### `POST /transcribe`

Transcribe un archivo completo. Body: `multipart/form-data`, campo `file`,
WAV mono PCM16 a 16 kHz.

```bash
curl -F "file=@audio.wav" http://localhost:8000/transcribe
# {"text": "hola como estas", "audio_ms": 990, "elapsed_ms": 205,
#  "drop_reason": null, "words": []}
```

`drop_reason` es lo que distingue "no había nada que transcribir" de "el
micrófono no está funcionando": sin él, un `text` vacío es indistinguible de un
micrófono roto.

### `WS /ws/transcribe`

Streaming. Protocolo:

1. El cliente abre el WebSocket y envía frames **binarios**: audio crudo
   PCM16LE mono a 16 kHz (sin cabecera WAV), en los chunks que le resulten
   cómodos — el servidor los reagrupa internamente en frames de 30 ms para
   el VAD.
2. El servidor responde con mensajes **JSON de texto** a medida que detecta
   habla:
   - `{"type": "partial", "text": "...", "audio_ms": 1200}` — transcripción
     parcial mientras la persona sigue hablando. Se calculan en segundo plano,
     sin frenar el procesamiento del audio, y nunca llegan después del final de
     su frase. `PARTIAL_INTERVAL_MS=0` los desactiva.
   - `{"type": "final", "text": "...", "audio_ms": 1710}` — transcripción final
     de un segmento de habla (silencio detectado, buffer máximo alcanzado o
     `end` del cliente).
3. El cliente puede enviar el texto `"end"` (frame de texto plano, no JSON)
   para forzar el cierre y la transcripción final del segmento en curso —
   útil al finalizar la grabación.

**El `end` siempre recibe respuesta.** Un `final` con `text` vacío, o incluso
sin audio pendiente (`audio_ms: 0`), es una respuesta válida: la frase se cerró
y no tenía palabras, típicamente porque el filtro de descarte no encontró voz.
Esto solo aplica al `end`: los finales que dispara el silencio por su cuenta se
descartan si vienen vacíos, porque en ese camino nadie está preguntando. Un
cliente que manda `end` y no atiende ese `final` se queda esperando un timeout
propio y acaba reportando como fallida una transcripción que nunca existió.

### Campos adicionales

`audio_ms` viaja siempre: son los milisegundos de audio del buffer que se
transcribió, y le sirven al cliente para distinguir una frase corta de una larga.
`words` (el instante de cada palabra) solo aparece con `WORD_TIMESTAMPS=true` y
hoy nadie lo lee: existía para el camino que representaba por fragmentos, que ya no
está. Ver la nota en `app/config.py` antes de prenderlo.

Ver `scripts/mic_test_client.py` para una implementación de referencia del
lado cliente (usa el micrófono del host, no el del contenedor).

## Desarrollo / prueba local sin la app móvil

```bash
pip install -r requirements-dev.txt
python scripts/mic_test_client.py
```

Esto abre el micrófono del host y transmite al servicio corriendo en Docker
(`ws://localhost:8000/ws/transcribe` por defecto), replicando el
comportamiento del script original de escritorio.

### Pruebas

La lógica de `app/session.py` —buffering, ventana de inicio, cierre por
silencio, recorte del silencio final, corte por pausa— está cubierta por
`scripts/verify_session.py`. Corre sin GPU y sin los fixtures de audio (que no
están en git): el VAD y el modelo se reemplazan por dobles, y lo que se prueba es
la lógica de este repo.

Se ejecuta en el contenedor del servicio, que es donde están las dependencias,
con la carpeta montada desde el host:

```bash
docker compose run --rm --no-deps -v "$PWD/scripts:/app/scripts" stt \
    python -m scripts.verify_session
```

`docker compose` se corre desde `../signall-backend`, que es donde está el
compose que levanta este servicio. Sale con código 1 si algo falla, así que sirve
como guardia de regresión: antes de tocar `SILENCE_TIMEOUT_MS` o `MAX_BUFFER_MS`,
correrlo.

Por qué importa: `SILENCE_TIMEOUT_MS` son 1500 ms de los ~4000 ms de latencia de
punta a punta, y bajarlo parte las frases donde la persona respira. Con la
prueba, ese número se cambia a la vista del efecto y no de memoria.

### Fixtures de audio

`fixtures/audio/*.json` describe 28 clips (texto, duración y palabras). Los
`.wav` **no están en git** — son unos 315 MB de audio binario — y se regeneran
con Piper:

```bash
python scripts/make_audio_fixtures.py --all
```

El script es determinista: el mismo texto produce siempre el mismo WAV, así que
las cifras de latencia de `docs/performance.md` son comparables entre corridas.

## Estructura

```
app/
  config.py       # settings desde variables de entorno
  metrics.py      # registro de métricas (/metrics y /metrics.json)
  transcriber.py  # carga de modelo, InferenceScheduler y transcripción de un buffer
  session.py      # buffering + VAD + partial/final por conexión WS
  main.py         # FastAPI: /health, /metrics, /transcribe, /ws/transcribe
scripts/
  mic_test_client.py        # cliente de prueba (no va en la imagen Docker)
  make_audio_fixtures.py    # genera los .wav de fixtures/audio con Piper
```
