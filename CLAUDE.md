# signall-stt — instrucciones para agentes

Este archivo es para quien trabaja en el repositorio, sea una persona o un agente. Dice qué es
esta pieza, cómo se trabaja y qué no se rompe. La puerta de entrada para alguien de afuera es el
[`README.md`](README.md); el detalle de la API y de las variables está en
[`docs/API-Y-CONFIGURACION.md`](docs/API-Y-CONFIGURACION.md).

## Qué es esta pieza

Servicio de reconocimiento de voz en español: **faster-whisper** más **WebRTC VAD**. Recibe
audio crudo por WebSocket, detecta cuándo hay voz, transcribe y devuelve `partial` mientras la
persona habla y un `final` cuando termina la frase. También acepta un archivo entero por
`POST /transcribe`.

**No** captura el micrófono, **no** corta el texto en oraciones, **no** traduce a glosas y **no**
decide qué se representa. Todo eso es del backend.

## El sistema: cuatro repositorios y el avatar

Este repositorio es **una pieza**. El sistema completo:

| Repositorio | Qué es | Puerto |
|---|---|---|
| [signall-android](https://github.com/sign-ai-organization/signall-android) | la app: captura el micrófono, envía por WebSocket y representa las señas | — |
| [signall-backend](https://github.com/sign-ai-organization/signall-backend) | FastAPI: WebSocket con la app, NLP de glosas, catálogo, caché semántico y Neon | 8080 |
| **signall-stt** (este) | audio → texto en tiempo real | 8000 |
| [signall-docs](https://github.com/sign-ai-organization/signall-docs) | los documentos de la tesis: SRS, SDD, memoria, plan de pruebas y plan de administración | — |
| [signall-avatar](https://github.com/sign-ai-organization/signall-avatar) (rama `dev`) | producción de las animaciones con Cascadeur | — |

**Los repositorios hermanos son fuentes de información, no cajas negras.** Antes de cambiar un
frame del WebSocket, un nombre de métrica o un umbral que cruce a otro repositorio, se lee el
otro lado: el contrato entre ellos son frames, y ningún compilador avisa cuando se rompe. Un
cambio que cruza el contrato no está terminado hasta que el otro lado lo refleja; si queda
pendiente, se dice en voz alta.

`signall-docs` es además **el repositorio de documentación de la tesis y el que manda para lo
escrito**. Los documentos formales son suyos y no hay copia en otro lugar. Cuando el código y un
documento no coinciden, no se elige por cuenta propia: se determina cuál de los dos está mal y se
pregunta al owner.

## Cada cambio se documenta en `signall-docs`

> **Cada cambio no trivial se documenta en `signall-docs`**: en `cambios/` (qué cambió y por qué)
> y, si toca un documento formal, se actualiza el `.tex` y se compila con `make`. Una sesión que
> termina sin dejar rastro ahí no está terminada.

Es un estándar, no una sugerencia. En este repositorio el rastro de trabajo es además
`docs/sessions/YYYY-MM-DD-<slug>.md`.

## El contrato con el backend

El consumidor es **solo** `signall-backend`, que llama este servicio en
`ws://stt:8000/ws/transcribe` dentro de la red de compose o en
`ws://localhost:8000/ws/transcribe` desde el host (`STT_WS_URL`). **La app Android nunca habla
con el STT.**

Lo que no se rompe:

- **El `final` siempre llega, incluso vacío.** El servicio tiene que responder a **todo** `end`
  con un `final`, aunque no se haya escuchado nada. Un `end` sin respuesta deja la frase colgada
  en el backend.
- **No se traduce nada acá.** Este servicio transcribe; la frase escrita que el cliente manda
  como `text` va derecho al backend y nunca pasa por acá. Si el STT devolviera texto que no vino
  del audio, el backend lo tomaría por voz.
- **El frame `capability` no es de este servicio**: lo manda el backend a la app al abrir la
  sesión. Acá solo viajan los frames de voz (`partial` y `final`).
- **Los nombres de métrica son `stt_*`** y viven en `app/metrics.py`. Un número que no se
  instrumentó en las dos partes no se puede comparar con nada.

Para cambiar un frame o un nombre de métrica, primero se lee el otro lado:
`signall-android/docs/SISTEMA.md` y `signall-backend/docs/android-integration.md`.

## Cómo se trabaja

```bash
# el servicio, solo
docker compose up --build
DEVICE=cpu COMPUTE_TYPE=int8 docker compose up --build   # sin GPU; quitar deploy.resources

# verificación de la segmentación y del filtro de voz (sin GPU)
docker compose run --rm --no-deps -v "$PWD/scripts:/app/scripts" stt \
    python -m scripts.verify_session
docker compose run --rm --no-deps -v "$PWD/scripts:/app/scripts" stt \
    python -m scripts.verify_non_speech

# pruebas y cobertura (contenedor descartable, instala requirements-dev.txt)
./scripts/cobertura.sh

# sintaxis
python -m compileall -q app scripts
```

El sistema completo —con el proxy TLS, que es parte del sistema— se levanta desde
`signall-backend`:

```bash
docker compose -f docker-compose.yml -f docker-compose.host.yml up -d
```

## Trampas que cuestan tiempo

- **Los `.wav` de `fixtures/` no están en git.** Se regeneran con
  `scripts/make_audio_fixtures.py`. Una prueba que dependa de ellos falla en un clon limpio si
  no se regeneran.
- **Los `scripts/` no se copian en la imagen.** Por eso las verificaciones se corren con
  `-v "$PWD/scripts:/app/scripts"`; sin el volumen, `python -m scripts.x` dice que el módulo no
  existe. Se corren **como módulo**: `python scripts/x.py` no resuelve `import app`.
- **El primer arranque descarga el modelo** de Hugging Face al volumen `hf-cache` y tarda
  varios minutos. `docker compose up` sin esperar a `/health` no significa que esté roto.
- **`SILENCE_TIMEOUT_MS` es el mayor término de la latencia.** Bajarlo "para que responda más
  rápido" parte las frases en las pausas. Antes de tocarlo, correr `verify_session`.
- **`AVG_LOGPROB_THRESHOLD` no se sube a `-0.8`**: descarta audio que el modelo transcribió bien
  (medido con "cómo estás" y "estoy triste").
- **`MAX_CONCURRENT_INFERENCE` es 1 a propósito.** Whisper serializa igual por debajo; con más
  de uno solo hay thrash.
- **La GPU es opcional y la CPU es lenta.** Si la GPU está declarada pero falla al cargar el
  modelo, el servicio cae a CPU solo.
- **Partes de `app/main.py` y `app/metrics.py` no se pueden cubrir sin GPU o servicios
  externos**: la cobertura de esas líneas es deuda conocida, no un olvido.

## Estilo

- Código y comentarios en inglés; los mensajes de usuario y los `PASS`/`FAIL` de las
  verificaciones, en español.
- Commit directo a `main`, con Conventional Commits. No se abren PRs.
- Los repositorios se nombran por nombre y URL, nunca por ruta absoluta de una máquina.
