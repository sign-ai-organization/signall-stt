# signall-stt

Servicio de Speech-to-Text (español) para el proyecto **SignAll**. Recibe audio del cliente
(la app Android) y devuelve texto transcrito en tiempo real, con
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) + WebRTC VAD.

No captura micrófono localmente: es un servicio HTTP/WebSocket en Docker. La captura la
hace el cliente, o `scripts/mic_test_client.py` para pruebas de escritorio.

Este servicio **solo transcribe**. No divide en oraciones, no traduce y no decide qué se
representa: eso es del backend.

## Cómo se corre

Lo normal es no levantarlo solo: el compose del backend lo construye y lo arranca.

```bash
cd ../signall-backend && docker compose up -d
```

Por separado:

```bash
docker compose up --build
```

El primer arranque descarga el modelo de Hugging Face (según `MODEL_SIZE`) y lo cachea en
un volumen, así que los siguientes son rápidos. El servicio queda en
`http://localhost:8000`.

Por defecto usa GPU (`DEVICE=cuda`) y necesita NVIDIA Container Toolkit. Sin GPU:

```bash
DEVICE=cpu COMPUTE_TYPE=int8 docker compose up --build
```

y hay que quitar el bloque `deploy.resources` de `docker-compose.yml`, porque Docker puede
fallar buscando un dispositivo que no existe. Aunque quede `DEVICE=cuda`, el servicio cae a
CPU solo si la GPU no está disponible al cargar el modelo.

## Cómo se verifica

```bash
curl -s http://localhost:8000/health           # {"status":"ok","model":"medium",...}
curl -s http://localhost:8000/metrics.json     # latencias y descartes
curl -F "file=@audio.wav" http://localhost:8000/transcribe
```

La lógica de segmentación tiene su propia prueba, que corre sin GPU y sin fixtures:

```bash
docker compose run --rm --no-deps -v "$PWD/scripts:/app/scripts" stt \
    python -m scripts.verify_session
```

Sale con código 1 si algo falla. Antes de tocar `SILENCE_TIMEOUT_MS` o `MAX_BUFFER_MS`,
correrla.

## Qué necesita

| Necesitas | Detalle |
|---|---|
| Docker + Compose | el servicio no se instala en el host |
| GPU NVIDIA | opcional: con `DEVICE=cpu` y `COMPUTE_TYPE=int8` funciona |
| Modelo de Whisper | se descarga la primera vez y queda en el volumen `hf-cache` |
| Puerto | `8000`, solo lo consume el backend |
| Fixtures de audio | opcionales: los `.wav` no están en git (315 MB) y se regeneran con `scripts/make_audio_fixtures.py` |

Las variables de entorno (`MODEL_SIZE`, `DEVICE`, los umbrales de silencio y VAD) y el
detalle del protocolo están en [`docs/API-Y-CONFIGURACION.md`](docs/API-Y-CONFIGURACION.md).

## Adónde ir después

| Si vas a tocar… | Lee |
|---|---|
| Variables, endpoints y protocolo del WebSocket | [`docs/API-Y-CONFIGURACION.md`](docs/API-Y-CONFIGURACION.md) |
| Rendimiento y mediciones | [`docs/performance.md`](docs/performance.md) |
| Cambios ya hechos | [`docs/CAMBIOSSTT.md`](docs/CAMBIOSSTT.md) |
| Cómo lo consume el backend | [`../signall-backend/docs/android-integration.md`](../signall-backend/docs/android-integration.md) |

Este repo no se commitea sin que el owner lo pida.
