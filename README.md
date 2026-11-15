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
cp .env.example .env      # perfil del plan de pruebas: medium en GPU
docker compose up --build
```

Queda en `http://localhost:8000`. El primer arranque descarga el modelo de Whisper desde
Hugging Face y lo deja en el volumen `hf-cache`, así que los siguientes son rápidos.

### CPU o GPU: se elige, no se detecta

El **default es el entorno del plan de pruebas**: Whisper `medium` en una GPU NVIDIA. El
modelo y el dispositivo se eligen con tres variables de entorno —`MODEL_SIZE`, `DEVICE` y
`COMPUTE_TYPE`—. Para una PC **sin GPU** (o con una que no alcanza) está el perfil de CPU en
`docker-compose.cpu.yml`: fija `small`/`cpu`/`int8`, quita la reserva del dispositivo NVIDIA y
compila la imagen sin las librerías de CUDA. La elección es **manual y explícita**: no hay
preguntas, no hay autodetección y no hay un segundo Dockerfile.

| Quiero… | Comando |
|---|---|
| **el default** (GPU, `medium`) | `docker compose up --build` |
| **sin GPU** | `docker compose -f docker-compose.yml -f docker-compose.cpu.yml up --build` |
| con GPU, otro modelo | `MODEL_SIZE=large-v3 docker compose up --build` |

| Archivo | `MODEL_SIZE` | `DEVICE` | `COMPUTE_TYPE` | Reserva NVIDIA | Librerías CUDA |
|---|---|---|---|---|---|
| `docker-compose.yml` (base, default) | `${MODEL_SIZE:-medium}` | `${DEVICE:-cuda}` | `${COMPUTE_TYPE:-float16}` | sí, driver `nvidia` | `requirements-gpu.txt` |
| `docker-compose.cpu.yml` (override) | `small` | `cpu` | `int8` | no (`!reset`) | `requirements.txt` |

El perfil de GPU necesita NVIDIA Container Toolkit (o Docker Desktop con soporte GPU
habilitado). Si se pide CUDA y la GPU falla al cargar el modelo, el servicio cae a CPU como
red de seguridad en tiempo de ejecución y `/health` publica el dispositivo efectivo; eso no
elige el perfil, que ya quedó declarado en el comando.

El consumidor es el backend, que lo llama en `ws://stt:8000/ws/transcribe` dentro de la red de
compose o en `ws://localhost:8000/ws/transcribe` desde la máquina anfitriona
(`STT_WS_URL`). La app Android nunca habla con este servicio.

## Requisitos

| Necesitas | Detalle |
|---|---|
| Docker + Compose | el servicio no se instala en el host |
| GPU NVIDIA | la del plan de pruebas (≥ 4 GB VRAM); sin GPU se usa el override `docker-compose.cpu.yml` |
| NVIDIA Container Toolkit | el default (GPU); no hace falta con `docker-compose.cpu.yml` |
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

## El resto del sistema

Este repositorio es **una pieza**. El sistema completo son
[signall-android](https://github.com/sign-ai-organization/signall-android) (la app),
[signall-backend](https://github.com/sign-ai-organization/signall-backend) (el WebSocket y las
glosas), este servicio y
[signall-docs](https://github.com/sign-ai-organization/signall-docs) (los documentos de la
tesis), más [signall-avatar](https://github.com/sign-ai-organization/signall-avatar) para las
animaciones.

Los repositorios hermanos son **fuentes de información**: antes de cambiar un frame del
WebSocket o una métrica hay que leer el otro lado, y el cambio no está terminado hasta que los
dos lo reflejan. **Cada cambio no trivial se documenta en `signall-docs`**, en `cambios/`: una
sesión que termina sin dejar rastro ahí no está terminada.

## Licencia

Este proyecto se distribuye bajo la licencia que defina la Pontificia Universidad Javeriana (pendiente de confirmación; por ahora, todos los derechos reservados). Ver [LICENSE](LICENSE).

## Trabajo de grado

SignAll es un trabajo de grado de la Pontificia Universidad Javeriana (Colombia). Para
citarlo: el equipo del trabajo de grado (2026). *SignAll: español hablado a Lengua de Señas Colombiana con un
avatar 3D*.
