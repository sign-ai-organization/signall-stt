# sign-ai-stt

Servicio de Speech-to-Text (español) para el proyecto SignAI. Recibe audio del
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
| `MODEL_SIZE` | `medium` | `tiny`, `base`, `small`, `medium`, `large-v3` |
| `DEVICE` | `cuda` | `cuda` o `cpu` |
| `COMPUTE_TYPE` | `float16` | `float16` (GPU) o `int8` (CPU) |
| `LANGUAGE` | `es` | Idioma para Whisper |
| `VAD_AGGRESSIVENESS` | `0` | 0 (permisivo) a 3 (estricto) |
| `SILENCE_TIMEOUT_MS` | `1500` | Silencio para cerrar un segmento |
| `MIN_RMS` | `0.008` | Filtro anti-alucinación por energía |

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
     la persona sigue hablando.
   - `{"type": "final", "text": "..."}` — transcripción final de un
     segmento de habla (silencio detectado o buffer máximo alcanzado).
3. El cliente puede enviar el texto `"end"` (frame de texto plano, no JSON)
   para forzar el cierre y la transcripción final del segmento en curso —
   útil al finalizar la grabación.

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

## Estructura

```
app/
  config.py       # settings desde variables de entorno
  transcriber.py  # carga de modelo + transcripción de un buffer
  session.py      # buffering + VAD + partial/final por conexión WS
  main.py         # FastAPI: /health, /transcribe, /ws/transcribe
scripts/
  mic_test_client.py  # cliente de prueba (no va en la imagen Docker)
```
