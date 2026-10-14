# sign-ai-stt

Servicio de Speech-to-Text (español) para el proyecto SignAI. Recibe audio del
cliente (app móvil) y devuelve texto transcrito en tiempo real, usando
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) + WebRTC VAD.

No captura micrófono localmente: es un servicio HTTP/WebSocket que corre en
Docker. La captura de audio la hace el cliente (la app móvil, o
`scripts/mic_test_client.py` para pruebas de escritorio).

## Perfil de tiempo real

Por defecto el servicio corre en un perfil pensado para que la app firme
mientras la persona habla, no después. Los números están medidos sobre la
GPU y las fixtures de `scripts/make_audio_fixtures.py`; el detalle está en
[`docs/performance.md`](docs/performance.md).

| | Antes | Ahora |
|---|---|---|
| Dejar de hablar → transcripción final | 1813 – 2099 ms | **p50 839 ms / p95 958 ms** |
| Inferencia de un parcial | 350 – 630 ms | **p50 92 ms / p95 172 ms** |
| Espera por la GPU | picos de 4 – 8 s | **p95 0.8 ms** |

Tres cosas lo explican:

- **Dos modelos.** `MODEL_SIZE` para los finales y `PARTIAL_MODEL_SIZE`
  (default `small`) para los parciales. Un parcial se tira a la basura en
  cuanto llega el final, así que no necesita la precisión de `medium`.
  Poner `PARTIAL_MODEL_SIZE` igual a `MODEL_SIZE` (o vacío) deja un solo
  modelo, que es lo que se hacía antes.
- **`InferenceScheduler`.** Whisper serializa igual por abajo
  (CTranslate2 con `num_workers=1`), así que sin un orden explícito el
  parcial que se despachó antes que el final se ejecuta antes, y el
  usuario espera los dos. Con el scheduler el final salta la cola.
- **Silencio más corto.** `SILENCE_TIMEOUT_MS` bajó de 1500 a 600. Es el
  piso por debajo del cual una pausa natural entre palabras parte la
  frase en dos, que para señas encadenables es el trade-off correcto.

`REALTIME=false` devuelve todos los defaults anteriores.

## Correr con Docker

El servicio se levanta normalmente desde el repo hermano, que lo incluye en
su `docker compose`:

```bash
cd ../sign-ai-backend && docker compose up --build
```

Para levantarlo solo desde este repo:

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
| `MODEL_SIZE` | `medium` | Modelo para los **finales**. `tiny`, `base`, `small`, `medium`, `large-v3` |
| `PARTIAL_MODEL_SIZE` | `small` | Modelo para los **parciales**. Vacío = el mismo que `MODEL_SIZE` |
| `PARTIAL_COMPUTE_TYPE` | `float16` | `float16` (GPU) o `int8` (CPU) |
| `DEVICE` | `cuda` | `cuda` o `cpu` |
| `COMPUTE_TYPE` | `float16` | `float16` (GPU) o `int8` (CPU) |
| `LANGUAGE` | `es` | Idioma para Whisper |
| `VAD_AGGRESSIVENESS` | `2` | 0 (permisivo) a 3 (estricto) |
| `SILENCE_TIMEOUT_MS` | `600` | Silencio para cerrar un segmento. El mayor término de la latencia |
| `PARTIAL_INTERVAL_MS` | `250` | Intervalo entre parciales; `0` los desactiva |
| `MIN_AUDIO_MS_FOR_PARTIAL` | `160` | Audio mínimo antes de poder parcial |
| `SPEECH_ONSET_WINDOW_MS` | `150` | Ventana de confirmación de inicio de habla |
| `SPEECH_ONSET_RATIO` | `0.7` | Proporción mínima de frames con habla en esa ventana |
| `WORD_TIMESTAMPS` | `true` | Mandar `words` con inicio/fin de cada palabra |
| `AVG_LOGPROB_THRESHOLD` | `-1.0` | Default de Whisper. **No subir a `-0.8`**: descarta audio que el modelo transcribió bien |
| `MAX_CONCURRENT_INFERENCE` | `1` | Inferencias simultáneas en la GPU |
| `MIN_RMS` | `0.008` | Filtro anti-alucinación por energía |
| `REALTIME` | `true` | `false` devuelve los defaults previos al modo tiempo real |

Ver `app/config.py` para la lista completa. Copiá `.env.example` a `.env` para
ajustarlas (`docker compose` lo lee solo).

## API

### `GET /health`

```json
{"status": "ok", "model": "medium", "device": "cuda", "language": "es"}
```

### `POST /transcribe`

Transcribe un archivo completo. Body: `multipart/form-data`, campo `file`,
WAV mono PCM16 a 16 kHz.

```bash
curl -F "file=@audio.wav" http://localhost:8000/transcribe
# {"text": "hola como estas"}
```

### `WS /ws/transcribe`

Streaming en tiempo real. Protocolo:

1. El cliente abre el WebSocket y envía frames **binarios**: audio crudo
   PCM16LE mono a 16 kHz (sin cabecera WAV), en los chunks que le resulten
   cómodos — el servidor los reagrupa internamente en frames de 30 ms para
   el VAD.
2. El servidor responde con mensajes **JSON de texto** a medida que detecta
   habla:
   - `{"type": "partial", "text": "..."}` — transcripción parcial mientras
     la persona sigue hablando. Se calculan en segundo plano, sin frenar
     el procesamiento del audio, y nunca llegan después del final de su
     frase. `PARTIAL_INTERVAL_MS=0` los desactiva.
   - `{"type": "final", "text": "..."}` — transcripción final de un
     segmento de habla (silencio detectado o buffer máximo alcanzado).
3. El cliente puede enviar el texto `"end"` (frame de texto plano, no JSON)
   para forzar el cierre y la transcripción final del segmento en curso —
   útil al finalizar la grabación.

### Campos adicionales

Los parciales y los finales llevan dos campos más que el backend usa para
decidir **qué seña firmar y cuándo**. Son aditivos: un cliente que los
ignore sigue funcionando, y uno que no los reciba degrada a una regla más
gruesa, no se rompe.

| Campo | Qué es |
|---|---|
| `words` | `[{w, s, e}, ...]`: cada palabra con su inicio y fin en ms, relativos al buffer de audio que la produjo. Una palabra está "terminada de decir" cuando su `e` quedó al menos cierto margen antes del final del audio. |
| `audio_ms` | Duración del audio capturado cuando se generó este mensaje. Es contra ese valor contra el que se compara cada `e`. |

`word_timestamps=false` los saca; el servicio sigue andando, el backend
degrada a "todo menos la última palabra".

## Métricas

```bash
curl -s 127.0.0.1:8000/metrics | less      # Prometheus
curl -s 127.0.0.1:8000/metrics.json         # lo mismo estructurado
```

| Métrica | Qué mide | Medido |
|---|---|---|
| `stt_end_to_final_ms` | dejar de hablar → final disponible | p50 839 / p95 958 ms |
| `stt_inference_ms{model=…}` | inferencia por modelo | `small` p50 92 ms, `medium` p50 249 ms |
| `stt_partial_queue_ms` | **espera por la GPU**. Antes daba picos de 4–8 s. | p50 0.4 / p95 0.8 ms |
| `stt_partial_lag_ms` | audio recibido sin procesar. Sube = saturación. | p50 −21 ms |
| `stt_vad_ms` | costo del VAD por frame de 30 ms | p50 0.07 ms |
| `stt_dropped_total{reason=…}` | transiciones descartadas | `rms`, `no_speech`, `logprob` |

`reason="logprob"` merece atención: cuenta audio que Whisper transcribió
**bien** y el filtro de confianza tiró igual. Con `AVG_LOGPROB_THRESHOLD`
en `-0.8` se comía "cómo estás" y "estoy triste" de los fixtures de
prueba, que salían como transcripción vacía. Volvió a `-1.0`.

## Generar audio de prueba

```bash
python3 -m venv .venv-tts && .venv-tts/bin/pip install piper-tts numpy
.venv-tts/bin/python scripts/make_audio_fixtures.py --all
```

Genera 28 WAV de habla en español (mono PCM16 a 16 kHz, que es lo que
exige el protocolo) cubriendo el vocabulario de LSC50, desde palabras
sueltas hasta conversaciones de tres frases. El mismo texto produce
siempre el mismo WAV, así que las cifras de latencia son comparables
entre corridas. Los `.json` al lado de cada WAV traen el texto, la
duración y los timestamps por palabra.

## Desarrollo / prueba local sin la app móvil

Con micrófono del host:

```bash
pip install -r requirements-dev.txt
python scripts/mic_test_client.py
```

Esto abre el micrófono del host y transmite al servicio corriendo en Docker
(`ws://localhost:8000/ws/transcribe` por defecto), replicando el
comportamiento del script original de escritorio.

Sin micrófono y sin nadie que hable, reproduciendo un WAV generado:

```bash
# el cliente del backend, que además cronometra y puede usar los 28 fixtures
cd ../sign-ai-backend
python scripts/verify_realtime_latency.py --all-fixtures
```

O desde el repo de la Android, todo junto (incluye el teléfono):

```bash
tools/verificar-tiempo-real.sh
```

## Estructura

```
app/
  config.py       # settings desde variables de entorno + perfil de tiempo real
  transcriber.py  # carga de modelo, InferenceScheduler y transcripción de un buffer
  session.py      # buffering + VAD + partial/final por conexión WS
  metrics.py      # registro Prometheus sin dependencias
  main.py         # FastAPI: /health, /metrics, /transcribe, /ws/transcribe
scripts/
  mic_test_client.py       # cliente de prueba (no va en la imagen Docker)
  make_audio_fixtures.py   # genera los 28 WAV de prueba con Piper
fixtures/audio/
  *.wav  *.json  index.json  # el audio es binario y no va en git; los .json sí
```
