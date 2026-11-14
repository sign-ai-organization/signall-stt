# Vuelta al flujo documentado: el STT transcribe, el backend traduce

- **Fecha:** 2026-10-15
- **Rama:** `flujo-oraciones`, desde `origin/main` (`7e724d8`, cuyo contenido es
  `e1da961`)
- **Plan:** `signall-docs/auditoria-flujo-2026-10-15.md` §6.2 y §6.2 bis

---

## Qué se hizo

La base `main` ya tenía el flujo documentado. El trabajo fue traer de `develop`
(`12c3fb7` + `2088a07`) lo que **no** es del perfil de tiempo real, deshacer lo
que sí lo es, y aplicar el arreglo de `flush()` que estaba sin commitear.

### Lo que se trajo, y por qué no depende del ritmo

| Qué | Por qué |
|---|---|
| `AVG_LOGPROB_THRESHOLD` −0.8 → **−1.0** | Con −0.8 el filtro descartaba frases que el modelo transcribía bien: "cómo estás" y "estoy triste" de los fixtures salían vacías. Es el default de whisper |
| Buffer con `list.append` en vez de `np.concatenate` por frame | ~68 MB de memcpy por utterance de 8 s, en el bucle de eventos, bloqueando la VAD de todas las conexiones (RNF-REL-01) |
| `InferenceScheduler` | Sin orden explícito el parcial que arrancó antes que el final se ejecutaba primero, con picos de 4–8 s de cola documentados |
| `TranscriptionResult` con `drop_reason` | Un `""` sin explicación es indistinguible de un micrófono roto |
| `_lag_ms` por foto del buffer (`2088a07`) | Antes mezclaba tiempo de micrófono apagado con saturación: con 20 s de silencio el número llegaba a 20000 ms |
| `app/metrics.py`, `/metrics`, `/metrics.json` | `marco.md` §6.1 lo pone como criterio de entrada a las rondas de prueba |
| Los 28 `fixtures/audio/*.json` y `make_audio_fixtures.py` | Son el corpus de prueba; los `.wav` no están en git y el script es determinista |

### Lo que se fue

El segundo modelo (`PARTIAL_MODEL_SIZE=small` para los parciales), los
timestamps por palabra en el payload, la bandera `REALTIME` con sus defaults
condicionales, y los seis valores que el perfil había cambiado: silencio
600 ms, parciales 250 ms, ventana de inicio 150 ms al 0.7, silencio final
120 ms y audio mínimo 160 ms.

**La bandera `REALTIME` era especialmente engañosa.** No era una reversión:
`docker-compose.yml` fijaba los cinco valores del perfil uno por uno, así que
`REALTIME=false` solo cambiaba una cosa de las seis (la ventana de inicio, vía
el default de `app/config.py`). Ahora la latencia se ajusta overrindiendo cada
variable, que es lo que hace el compose, y los seis valores documentados están
explícitos en el compose con un comentario que lo dice.

### `flush()` responde siempre

El arreglo que estaba en el stash (`stash@{0}`, sin commitear) y que §6.2 marca
como importante. `flush()` pasó a devolver un `dict` siempre, no `dict | None`:
un `final` con `text: ""` o incluso sin audio pendiente (`audio_ms: 0`) es una
respuesta válida a un `end`, y la única forma que tenía el cliente de saber que
la frase se cerró.

El camino por silencio (`feed`) sigue descartando los vacíos: ahí nadie está
preguntando y mandarlos sería ruido en la conexión.

### `WORD_TIMESTAMPS` queda apagado, con el código puesto

Decisión que vale registrar porque la alternativa era borrarlo: `WORD_TIMESTAMPS`
queda en `false` y `WordTiming` / `_word_timings` siguen en el código. Nadie lee
`words`: existían para que el backend decidiera qué palabra ya había terminado
de decir, que era la regla del camino por fragmentos.

Se conservaron por dos razones concretas: es la forma de volver a pedirlos sin
tocar nada (el costo medido está en `docs/performance.md`), y `POST /transcribe`
los devuelve, que es cómo se diagnostica una transcripción rara. El backend los
parsea de forma tolerante, así que con el flag apagado el payload es
exactamente el que espera el contrato. Si en algún día no los quiere nadie, se
borran en `app/config.py` y en `TranscriptionResult.to_payload`, y nada más.

---

## Verificación

- Pila completa con el STT reconstruido y el backend de la fase 1, sobre los
  28 fixtures reales de `fixtures/audio/`:
  - `como_estas` → `final` `'como estas.'` (`audio_ms: 990`, sin `words`)
  - `muchas_personas` → 1 parcial + `final` `'cuántas personas son.'`
  - `end` sin audio → `{"type": "final", "text": "", "audio_ms": 0}`
  - audio de silencio → 0 frames (descartado, correcto: nadie preguntó)
- A través del backend (`ws://127.0.0.1:8080/ws/translate`):
  - voz: 2 parciales + `final`, y **0 `segment`**: la voz no se traduce sola
  - la misma frase como `{"type":"text"}` → `segment 0/1` con glosa `PERSONAS`
  - `{"type":"end"}` sin audio → `{"type":"transcript","text":"","final":true}`
- `/health`: `{"model": "medium", "silence_timeout_ms": 1500, ...}` — un solo
  modelo y el silencio del diagrama 6.3.
- `/metrics.json`: `stt_final_ms` p50 205 ms, `stt_partial_queue_ms` p50 0.6 ms,
  `stt_final_lag_ms` p50 0.0 (el fix de `2088a07` funcionando),
  `stt_final_total{kept="0",reason="flush"}` contando el final vacío.

## Lo que quedó anotado y no se arregló

`app/metrics.py` no distingue los finales que se descartaron por el filtro de
los que llegaron vacíos porque no había audio: en el segundo caso la etiqueta
del histograma es `model="?"` porque no corrió ninguna inferencia. Es
información correcta, pero confunde al leer `stt_final_ms`. Se deja así porque
arreglarlo es cambiar el contrato de métricas y eso merece su propio cambio.

## Autorización de commit para esta rama

El 15 de octubre el owner autorizó commitear **sin pedir permiso** en la rama
`flujo-oraciones` de los tres repos, mientras el trabajo siga siendo local y en
esa rama. Es una excepción acotada a este trabajo: la regla de `AGENTS.md` ("nada
de commits en los hermanos sin que el owner lo pida", PR contra `develop`) sigue
vigente para cualquier otra cosa, y `main` no se toca hasta que el owner decida
el destino del trabajo.

## Pendiente del owner

1. **Remedir.** Las cifras del perfil de tiempo real (p50 839 ms de fin del
   habla a final) no son comparables con las de ahora: el silencio era de 600 ms
   en vez de 1500, y 600 ms es el término que se quitó. Ver §8 de
   `docs/performance.md`.
2. **La app.** `{"type":"end"}` al apagar el micrófono y atender el `final`
   vacío es el otro lado del arreglo; hasta que la app no lo haga, el `end`
   sigue sin respuesta útil para ella.