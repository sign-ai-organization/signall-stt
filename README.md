# signall-stt

Servicio de reconocimiento de voz en español para el proyecto **SignAll**, construido con
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) y WebRTC VAD.

## Qué es

Un servicio HTTP y WebSocket que recibe audio, detecta cuándo hay voz, lo transcribe y
devuelve el texto en streaming: parciales mientras la persona habla y un final cuando
termina la frase. El modelo se ejecuta en el servidor, así que el cliente solo manda audio
crudo.

Es la primera pieza de la cadena: entrega texto, que después
[signall-backend](https://github.com/sign-ai-organization/signall-backend) convierte en glosas
y [signall-android](https://github.com/sign-ai-organization/signall-android) representa con el
avatar.

## Enfoque

**Hace:** recibe audio PCM16LE mono a 16 kHz por `WS /ws/transcribe`, lo agrupa con el
detector de voz, transcribe los segmentos con Whisper y responde `partial` y `final` en JSON.
También acepta un archivo completo por `POST /transcribe` y expone `/health`, `/metrics` y
`/metrics.json`.

**No hace:** no captura el micrófono (el audio lo manda el cliente), no corta el texto en
oraciones, no traduce a señas y no decide qué se representa: eso es del backend. Tampoco
guarda historial ni audio.

## Cómo se usa

El servicio está pensado para correr en Docker. La forma habitual es que lo levante el
`docker-compose.yml` de [signall-backend](https://github.com/sign-ai-organization/signall-backend),
que lo construye y lo conecta por la red interna. Para correrlo solo:

```bash
git clone https://github.com/sign-ai-organization/signall-stt
cd signall-stt
docker compose up --build
```

Queda en `http://localhost:8000`. El primer arranque descarga el modelo de Whisper desde
Hugging Face y lo deja en el volumen `hf-cache`, así que los siguientes son rápidos.

Por defecto usa GPU (`DEVICE=cuda`, `COMPUTE_TYPE=float16`) y necesita NVIDIA Container
Toolkit. Sin GPU:

```bash
DEVICE=cpu COMPUTE_TYPE=int8 docker compose up --build
```

En ese caso también hay que quitar el bloque `deploy.resources` del `docker-compose.yml`,
porque Docker intenta reservar un dispositivo que no existe. Si la GPU está declarada pero
falla al cargar el modelo, el servicio cae a CPU solo.

El consumidor es el backend, que lo llama en `ws://stt:8000/ws/transcribe` dentro de la red de
compose o en `ws://localhost:8000/ws/transcribe` desde la máquina anfitriona
(`STT_WS_URL`). La app Android nunca habla con este servicio.

## Requisitos

| Necesitas | Detalle |
|---|---|
| Docker + Compose | el servicio no se instala en el host |
| GPU NVIDIA | opcional; con `DEVICE=cpu` y `COMPUTE_TYPE=int8` funciona |
| NVIDIA Container Toolkit | solo si se usa GPU |
| Modelo de Whisper | se descarga en el primer arranque y queda en el volumen `hf-cache` |
| Puerto | `8000` |
| Fixtures de audio | opcionales: los `.wav` no están en git y se regeneran con `scripts/make_audio_fixtures.py` |

Las variables de entorno (`MODEL_SIZE`, `DEVICE`, umbrales de silencio y de VAD) están
documentadas en [`docs/API-Y-CONFIGURACION.md`](docs/API-Y-CONFIGURACION.md).

## Estructura

```
app/           servicio: sesión de audio, detector de voz, transcritor y métricas
tests/         suite de pytest que cubre app/
scripts/       verificaciones sin GPU, cliente de micrófono y generador de fixtures
fixtures/      definiciones de los audios de prueba; los `.wav` se generan localmente
docs/          API, variables de entorno y mediciones
```

## Cómo se verifica

```bash
curl -s http://localhost:8000/health        # {"status":"ok","model":"medium",...}
curl -s http://localhost:8000/metrics.json  # latencias y descartes
curl -F "file=@audio.wav" http://localhost:8000/transcribe
```

La lógica de segmentación y el filtro de voz tienen su propia verificación, que corre sin GPU
y sin fixtures. Las dos se ejecutan dentro de la imagen, porque los scripts no se copian en
ella:

```bash
docker compose run --rm --no-deps -v "$PWD/scripts:/app/scripts" stt \
    python -m scripts.verify_session

docker compose run --rm --no-deps -v "$PWD/scripts:/app/scripts" stt \
    python -m scripts.verify_non_speech
```

Las dos salen con código 1 si algo falla. Antes de tocar `SILENCE_TIMEOUT_MS` o
`MAX_BUFFER_MS`, conviene correr la primera.

La cobertura de `app/` se mide con:

```bash
./scripts/cobertura.sh
```

Corre los dos scripts y la suite de `tests/` en un contenedor descartable, instala allí las
dependencias de `requirements-dev.txt` y deja `coverage.xml` en la raíz. No necesita GPU. La
misma suite se puede correr en el host con `pytest -q` si se tienen esas dependencias
instaladas.

## Licencia

Este proyecto se distribuye bajo la licencia {{LICENCIA}}. Ver [LICENSE](LICENSE).

## Trabajo de grado

SignAll es un trabajo de grado de la Pontificia Universidad Javeriana (Colombia). Para
citarlo: {{AUTOR}} ({{AÑO}}). *SignAll: español hablado a Lengua de Señas Colombiana con un
avatar 3D*.
