# signall-stt — instrucciones para agentes

Pieza de SignAll: reconocimiento de voz en español con faster-whisper y WebRTC VAD. Empieza en el
[`README.md`](README.md); la API, en [`docs/API-Y-CONFIGURACION.md`](docs/API-Y-CONFIGURACION.md).

## Qué hace y qué no hace

- Recibe audio PCM16LE mono a 16 kHz por `WS /ws/transcribe`, lo corta con el detector de voz,
  transcribe cada segmento y responde `partial` mientras la persona habla y `final` al terminar.
- Acepta un archivo entero por `POST /transcribe` y expone `/health`, `/metrics` y `/metrics.json`.
- Solo transcribe. No captura el micrófono, no corta el texto en oraciones, no traduce a glosas y
  no decide qué se representa: eso es del backend. Tampoco guarda audio ni historial.

## Su lugar entre los repositorios

Los cinco repositorios viven en https://github.com/sign-ai-organization: signall-android, la app
que captura el micrófono y representa las señas; signall-backend, el WebSocket con las glosas y el
catálogo; este, audio a texto en el puerto 8000; signall-docs, los documentos de la tesis; y
signall-avatar, en la rama `dev`.

El único consumidor es el backend, que llama en `ws://stt:8000/ws/transcribe` dentro de la red de
compose o en `ws://localhost:8000/ws/transcribe` desde el host (`STT_WS_URL`). La app Android
nunca habla con este servicio. Los hermanos son fuentes de información: antes de cambiar un frame,
un umbral o una métrica se lee el otro lado, y el cambio no está terminado hasta que lo refleja.

## El contrato con el backend

- A todo `end` responde un `final`, aunque vaya vacío. Un `end` sin respuesta deja la frase
  colgada en el backend.
- Acá no se traduce nada: la frase escrita que el cliente manda como `text` va derecho al backend
  y nunca pasa por acá, porque el backend la tomaría por voz.
- El frame `capability` no es de este servicio: lo manda el backend a la app. Acá solo viajan
  `partial` y `final`.
- Las métricas llevan prefijo `stt_*` y salen del registro en `app/metrics.py`: `stt_inference_ms`,
  `stt_final_ms`, `stt_partial_ms`, `stt_partial_queue_ms`, `stt_end_to_final_ms` y
  `stt_dropped_total{reason}`. Un número que no se instrumentó en las dos partes no se compara.
- Para cambiar un frame o una métrica se leen `signall-android/docs/SISTEMA.md` y
  `signall-backend/docs/android-integration.md`.

## Los dos perfiles

El default es el entorno del plan de pruebas: `medium` en una GPU NVIDIA con `float16`, y la GPU
reservada en compose. No se cambia sin decisión del owner.

```bash
docker compose up --build                                                  # default: GPU, medium
docker compose -f docker-compose.yml -f docker-compose.cpu.yml up --build  # sin GPU
```

- `docker-compose.cpu.yml` es el override para un equipo sin GPU o con una que no alcanza: fija
  `small`/`cpu`/`int8`, quita la reserva con `deploy: !reset` y compila con `requirements.txt`.
- `requirements-gpu.txt` incluye `requirements.txt` y suma `nvidia-cublas-cu12` y
  `nvidia-cudnn-cu12`: es lo que instala la imagen por defecto, por el `ARG REQUIREMENTS_FILE` del
  `Dockerfile`, y `ctranslate2` solo los carga con `DEVICE=cuda`.
- Nada detecta el hardware: el perfil se declara en el comando. `/health` publica `device` y
  `requested_device`; si CUDA falla al cargar, el modelo cae a CPU y eso no cambia el perfil
  declarado.

## Cómo se trabaja y se verifica

```bash
curl -s http://localhost:8000/health      # y /metrics.json, para latencias y descartes
./scripts/cobertura.sh          # pytest y los dos scripts, en contenedor descartable
tools/calidad/verificar.sh      # compilación y escaneo de credenciales

# segmentación y filtro de voz, sin GPU
docker compose run --rm --no-deps -v "$PWD/scripts:/app/scripts" stt \
    python -m scripts.verify_session
docker compose run --rm --no-deps -v "$PWD/scripts:/app/scripts" stt \
    python -m scripts.verify_non_speech
```

El sistema completo, con el proxy TLS, se levanta desde `signall-backend` con
`docker compose -f docker-compose.yml -f docker-compose.host.yml up -d`. Cada cambio no trivial se
documenta en `signall-docs/cambios/` y deja su rastro en `docs/sessions/`; si toca un documento
formal, se edita el `.tex` y se compila con `make`.

## Trampas que cuestan tiempo

- Los `.wav` de `fixtures/` no están en git. Se regeneran con `scripts/make_audio_fixtures.py`;
  una prueba que dependa de ellos falla en un clon limpio.
- Los `scripts/` no se copian en la imagen: hay utilidades de escritorio con dependencias que el
  servicio no tiene. Se montan con `-v` y se corren como módulo, porque `python scripts/x.py` no
  resuelve `import app`.
- El primer arranque descarga el modelo de Hugging Face al volumen `hf-cache` y tarda varios
  minutos. Un `up` que todavía no responde en `/health` no está roto.
- `SILENCE_TIMEOUT_MS` es el mayor término de la latencia; bajarlo parte las frases en las pausas,
  y una frase partida son dos señas. Antes de tocarlo, correr `verify_session`.
- `AVG_LOGPROB_THRESHOLD` no se sube a `-0.8`: descarta audio que el modelo transcribió bien.
- `MAX_CONCURRENT_INFERENCE` es 1 a propósito: Whisper serializa igual por debajo.
- Partes de `app/main.py` y `app/metrics.py` no se cubren sin GPU ni servicios externos: esa
  cobertura es deuda conocida, no un olvido.

## Estilo

- Código y comentarios en inglés; los mensajes de usuario y los `PASS`/`FAIL`, en español.
- Commit directo a `main` con Conventional Commits (`feat`, `fix`, `docs`, `refactor`, `chore`,
  `test`). No se abren PRs.
- Los repositorios se nombran por nombre y URL, nunca por ruta absoluta de una máquina.
