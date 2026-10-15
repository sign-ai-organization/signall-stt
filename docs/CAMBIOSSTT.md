# CAMBIOS — STT (`flujo-oraciones`, 2026-10-15)

Qué cambió en este repo para acompañar el paso del flujo **por fragmentos** al
flujo **por oraciones**, y qué tiene que hacer el resto del sistema con esto.

**Rama:** `flujo-oraciones`, desde `origin/main`. **Sin mergear.**

> **Base de la rama, y es deliberada.** `origin/main` de este repo es **anterior** al
> trabajo de tiempo real, que vive en `origin/develop` (4 commits: el perfil de dos
> modelos, `PARTIAL_INTERVAL_MS=250`, `SILENCE_TIMEOUT_MS=600`, …). Volver a `main`
> y **dejar atrás** ese perfil es decisión del owner, escrita en
> `Revisión de develop.md` (nota en la raíz de `sign-ai-android`, 2026-10-15):
>
> > **Base recomendada.** `origin/main` del backend ya contiene lo útil […] En STT,
> > el commit `e1da961`.
>
> Por eso esta rama no parte del perfil de tiempo real: define los valores finales
> desde el principio. La tabla de más abajo dice "antes / ahora" por claridad, pero
> el "antes" **nunca estuvo en esta línea**. **No rebasar sobre `develop` sin preguntar**
> — reintroduce el perfil de dos modelos, que es justo lo que vino a desarmar.

---

## 1. La unidad es la oración, así que el STT cierra frases completas

El perfil de tiempo real existía para producing **parciales rápidos** (`small`)
mientras un `medium` cerraba la frase: el backend traducía cada parcial y el
avatar firmaba a mitad de camino. Con oraciones eso no hace falta —solo importa el
`final`—, así que hay **un solo modelo** y el silencio queda puesto en el
valor del SDD.

| | Antes (perfil de tiempo real) | Ahora |
|---|---|---|
| Modelos | dos: `small` + `medium` | **uno** (`medium`) |
| `SILENCE_TIMEOUT_MS` | 600 | **1500** (el del diagrama 6.3 del SDD) |
| `PARTIAL_INTERVAL_MS` | 250 | 400 |
| `SPEECH_ONSET_WINDOW_MS` / `_RATIO` | 150 ms / 0.7 | 300 ms / 0.9 |
| `FINAL_TRAILING_SILENCE_KEEP_MS` | 120 ms | 300 ms |
| `MIN_AUDIO_MS_FOR_PARTIAL` | 160 ms | 200 ms |
| `WORD_TIMESTAMPS` | `true` | **`false`** |
| Variable `REALTIME` | cortaba el perfil | **ya no existe** |

**Los parciales se quedan** (`PARTIAL_INTERVAL_MS=400`): son RF-STT-02, del 30 de
septiembre, anteriores al perfil. El texto se muestra progresivo porque es un
requisito, no porque sirva para firmar antes — en el flujo por oraciones el
partial no dispara nada.

`WORD_TIMESTAMPS=false` porque con la oración completa no hay por qué alinear
palabras: las palabras las arma el divisor de oraciones del backend.

### Por qué 1500 ms y no menos

1500 ms es el mayor término de la latencia —son los ms que se espera después de
que la persona terminó de hablar—, pero bajarlo a 600 parte las frases en los
puntos donde la persona respira, y una frase partida son **dos oraciones** y por
lo tanto dos señas. El valor está literal en el diagrama 6.3 del SDD.

---

## 2. `flush()` responde **siempre** — el fix que más fácil se rompe

`f0199ba`: un `end` ahora produce siempre un `final`, incluso vacío.

Esto no es cosmético. Es lo que hace que el botón del micrófono de la app pueda
apagarse: al apagar se manda `end`, y **la app espera ese `final`** antes de dar
por cerrada la frase dictada. Sin respuesta, el cliente se come su propio timeout
y reporta como fallida una frase que nunca existió — el mismo síntoma que antes
se veía como "queda en Traduciendo... para siempre".

Consecuencia en el cliente: un `final` **vacío** es el caso normal de "el
micrófono estaba prendido y no se oyó nada", y la app lo maneja como tal
(`onEmptyFinal`), sin error.

**Verificado**: los 28 fixtures por la pila completa, más el caso de `end` con
cero audio detrás.

---

## 3. Lo que se conserva a propósito

- **`InferenceScheduler`**: el scheduler de inferencia sigue. Sin él, un parcial
  puede retrasar un final 4–8 s (`stt_partial_queue_ms` es el guard de
  regresión de eso).
- **Diagnóstico de descartes** (`drop_reason`): `stt_dropped_total{reason=...}`.
  `reason="logprob"` caza al filtro de confianza comiéndose audio que Whisper
  transcribió bien, que era la causa de frases que desaparecían.
- **Métricas**: `stt_end_to_final_ms`, `stt_inference_ms`,
  `stt_partial_lag_ms`, `stt_partial_queue_ms`. Los nombres **no** cambiaron y no
  deben cambiar: son los mismos que lee `tools/verificar-tiempo-real.sh` y los
  mismos que se comparan entre corridas.
- **`_lag_ms`** (audio recibido menos audio procesado): sin ese dato, un final
  vacío es indistinguible de un micrófono roto.

---

## 4. Fixtures de audio

`scripts/make_audio_fixtures.py` (nuevo) genera los **28 WAV** con Piper. Los
`.json` están en git; los `.wav` **no** (1,5 MB de binario), y se regeneran:

```bash
./venv/bin/python scripts/make_audio_fixtures.py --all
```

El script produce **siempre el mismo WAV para el mismo texto**, así que las
cifras de latencia de este repo son comparables entre corridas. Eso no era
ciego: con un TTS distinto, cada corrida sería un experimento distinto.

---

## 5. Cómo verificar

```bash
docker compose -f ../sign-ai-backend/docker-compose.yml up -d
curl -s 127.0.0.1:8000/health
# {"status":"ok","model":"medium","device":"cuda","language":"es","silence_timeout_ms":1500}
```

Los parciales, los descartes y el atraso, en los logs del contenedor:

```bash
docker compose -f ../sign-ai-backend/docker-compose.yml logs -f stt | grep -E '\[timing\]|\[rms\]|descartado'
```

Desde el repo de Android, las tres mediciones que usan los 28 fixtures:

```bash
tools/verificar-tiempo-real.sh contrato   # divisor de oraciones + protocolo
tools/medir-primera-sena.py               # de dónde sale la primera seña
tools/medir-retraso.py                    # ¿el avatar alcanza a firmar mientras hablás?
```

---

## 6. Lo que este repo necesita del resto

- **El backend tiene que tolerar un `final` vacío.** No es un error ni una
  desconexión: es "no se escuchó nada".
- **El teléfono tiene que esperar el `final` antes de mandar nada.** Es el paso de
  confirmación (RF-STT-03): el texto dictado espera a que el usuario lo mande.
- **Los nombres de métrica tienen que coincidir.** `rt_first_sign_ms` y
  `stt_end_to_final_ms` miden tramos distintos del mismo camino y solo se suman
  si los dos lados los instrumentaron.

---

## Pendiente

1. **Falta remedir con `SILENCE_TIMEOUT_MS=1500`.** Las cifras del perfil de
   tiempo real —fin del habla → final p50 839 ms / p95 958 ms, inferencia de un
   parcial p50 92 ms, espera por la GPU p95 0.8 ms— **no son comparables** con
   las de ahora: el silencio era de 600 ms en vez de 1500, y 600 ms es
   exactamente el término que se quitó. La entrada 7 de `docs/performance.md`
   (1813–2099 ms de fin del habla a final disponible) sigue siendo la referencia.
   Con los valores de ahora, sobre los fixtures, el final sale entre 205 y 227 ms
   de inferencia y la espera por la GPU es de ~1 ms.
3. **Medir la cadena completa, no este repo solo.** `medir-primera-sena.py` mide
   los dos tramos juntos: de la transcripción final a la primera seña hay
   **p50 273 ms / p95 422 ms** con `qwen3:4b-instruct` en Ollama local y la caché
   semántica vacía. Lo que percibe el usuario, del primer sample de audio, es
   **p50 4010 ms**, de los cuales **3709 ms son este STT**. El pipeline de
   traducción no es el cuello de botella; el silencio más la transcripción, sí.