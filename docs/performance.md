# Bitácora de rendimiento

Mediciones de latencia del servicio STT y efecto de cada cambio. Los
tiempos vienen de las líneas `[timing]` que `app/session.py` escribe en
los logs del contenedor. Medido con `scripts/mic_test_client.py`, modelo
`medium` en GPU (RTX 3050).

Entradas más recientes al final.

---

## 1. Línea base y VAD

**Fecha:** 2026-10-01

Mismas frases habladas, con pausas largas entre ellas:

| | `VAD_AGGRESSIVENESS=0` | `VAD_AGGRESSIVENESS=2` |
|---|---:|---:|
| Frases con pausa cerradas por silencio | 1 de 5 | 8 de 8 |
| Finales vacíos (solo ruido) | 5 | 1 |
| Cortes forzados a los 8 s (`buffer_max`) | 4 | 2 |
| Fin de la habla → final (cierres por silencio) | 1877 ms | 1880–2047 ms |

- Con 0, el ruido de fondo se clasificaba como habla: las pausas no
  cerraban el segmento y los finales llegaban por el corte de 8 s,
  partiendo frases.
- Con 2, cada pausa cierra el segmento: el final llega ~1.9–2.0 s
  después de dejar de hablar (1500 ms de silencio + 400–575 ms de
  transcripción).
- Los 2 cortes restantes son frases dichas sin pausa durante más de 8 s
  (`MAX_BUFFER_MS`).
- Los parciales cuestan 350–630 ms de GPU cada ~400 ms y retrasan el
  procesamiento del audio (atraso de hasta 4–6 s). Hubo picos aislados
  de 6–8 s en un solo parcial.

**Cambio:** `VAD_AGGRESSIVENESS` por defecto pasa a 2. Además,
`SILENCE_TIMEOUT_MS` por defecto en `config.py` pasa de 600 a 1500, para
coincidir con `docker-compose.yml` y el README.

---

## 2. Parciales desactivados (prueba)

**Fecha:** 2026-10-01

| | Con parciales | Sin parciales |
|---|---:|---:|
| Atraso máximo del procesamiento | 5817 ms | ~0 ms |
| Picos de transcripción de 6–8 s | sí | no |
| Fin de la habla → final, peor caso | 7742 ms | 2635 ms |
| Fin de la habla → final, típico | 1880–2047 ms | 2148–2635 ms |

- Sin parciales, el VAD procesa el audio en tiempo real y desaparecen
  los picos.
- Los finales tardan más en transcribirse con duraciones de audio
  similares (5.0 s: 409 → 803 ms), y el primero tras arrancar tarda
  1165 ms. Hipótesis sin confirmar: arranque de CUDA en la primera
  inferencia, y GPU que baja su frecuencia al quedar ociosa entre
  frases.

**Conclusión:** desactivar los parciales incumple RF-STT-02
(transcripción progresiva: el texto debe actualizarse mientras el
usuario habla). Se mantienen los parciales, pero sin bloquear el
procesamiento del audio: uno a la vez en segundo plano, descartados si
su final ya salió y sin generarse durante el silencio.

---

## 3. Parciales en segundo plano

**Fecha:** 2026-10-01
**Cambio:** la transcripción parcial corre en segundo plano y ya no
detiene el procesamiento del audio. Nunca hay más de un parcial a la
vez, un parcial que termina después del final de su frase se descarta,
y no se generan parciales sin habla nueva. Activos por defecto
(`PARTIAL_INTERVAL_MS=400`).

| | Parciales bloqueantes | Sin parciales | Parciales en segundo plano |
|---|---:|---:|---:|
| Texto en vivo (RF-STT-02) | sí | no | **sí** |
| Atraso máximo del procesamiento | 5817 ms | ~0 ms | **~0 ms** |
| Picos de 6–8 s | sí | no | **no** |
| Fin de la habla → final (con voz) | 1880–2047 ms | 2148–2635 ms | **1862 ms** |

- El atraso se mantuvo entre −28 y −30 ms toda la sesión: el VAD
  procesa el audio en tiempo real aunque Whisper esté calculando un
  parcial.
- En esta sesión no se observó que un parcial en curso retrasara un
  final.

**Hallazgo: se pierden frases con voz.** En dos frases los parciales
tenían texto (transcripciones de 341–692 ms), pero el final salió vacío
(`transcripcion=0ms`). `transcribe_buffer` descarta el buffer completo
si su energía promedio (RMS) es menor que `MIN_RMS`, y el buffer del
final incluye siempre los 1500 ms de silencio que lo cerraron, lo que
baja el promedio por debajo del umbral. Los parciales no lo sufren
porque no llevan esa cola de silencio. Algunos "finales vacíos" de las
entradas 1 y 2 probablemente eran frases perdidas por esta misma causa,
no solo ruido.

---

## 4. Recorte del silencio final y finales vacíos

**Fecha:** 2026-10-01
**Cambio:** antes de transcribir el final se recorta el silencio que lo
cerró, conservando 300 ms (`FINAL_TRAILING_SILENCE_KEEP_MS`). Los
finales sin texto ya no se envían al cliente (siguen en el log).

- 6 de 6 frases habladas llegaron con texto: ninguna perdida.
- 3 finales de solo ruido se descartaron sin llegar al cliente.
- Whisper transcribe menos audio por final (p. ej., 3.8 s → 3.4 s).
- Fin de la habla → final: 1855–2284 ms en casos normales.

**Hallazgos para el siguiente cambio:**

- **Ruido al inicio de la frase.** El VAD a veces toma un solo frame de
  ruido como inicio de habla, y el buffer acumula hasta ~3 s de ruido
  antes de que la persona hable. Efectos: los parciales salen vacíos
  hasta que la voz domina la energía promedio (el texto tarda en
  aparecer), el ruido consume parte de los 8 s de `MAX_BUFFER_MS`
  (cortes antes de tiempo) y aparecen "frases" de solo ruido de hasta
  7.7 s.
- **Picos en parciales diminutos.** Los dos picos más grandes (8426 ms y
  4005 ms) fueron parciales de 0.2 s de audio. Mientras duran, ocupan la
  GPU y retrasan el final siguiente (3237 ms en esta sesión).

---

## 5. Confirmación del inicio de la habla

**Fecha:** 2026-10-01
**Cambio:** una frase ya no empieza por un solo frame marcado como
habla. Empieza cuando al menos el 90 % de los últimos 300 ms son habla
(`SPEECH_ONSET_RATIO`, `SPEECH_ONSET_WINDOW_MS`), y esos 300 ms se
incluyen en el buffer para no recortar la primera sílaba.

- El texto aparece desde el primer parcial (0.3 s) en casi todas las
  frases; antes había hasta ~3 s de parciales vacíos al inicio.
- Sin "frases" largas de solo ruido (una de 0.6 s, descartada).
- Sin picos de 4–8 s; el mayor fue de ~2 s, en la frase más larga.
- Los 2 cortes por `buffer_max` fueron de frases realmente largas sin
  pausa: ya no los provoca el ruido inicial.

**Hallazgo: el filtro `MIN_RMS` sigue perdiendo frases.** Una frase con
parciales con texto terminó en un final vacío. `transcribe_buffer`
compara el volumen promedio de todo el buffer contra `MIN_RMS` (0.008).
Con una voz apenas por encima del umbral, cualquier tramo bajo dentro
de la frase (pausas, ruido tras hablar) baja el promedio y la frase
completa se descarta. El recorte del silencio final lo redujo, pero no
lo resuelve: el problema es usar un promedio.

---

## 6. Filtro de volumen por tramos y precalentamiento del modelo

**Fecha:** 2026-10-01
**Cambios:**
- `MIN_RMS` ya no se compara contra el volumen promedio del buffer, sino
  contra el percentil 90 del volumen de sus tramos de 30 ms
  (`MIN_RMS_PERCENTILE`): una frase con voz siempre tiene tramos
  fuertes, aunque tenga pausas.
- Al arrancar se hace una transcripción de prueba antes de aceptar
  conexiones (660 ms).

**Resultados:**
- 9 de 9 frases llegaron con texto, incluida la primera de la sesión.
- La primera transcripción de la sesión baja de ~1000–1300 ms a 520 ms.
- Fin de la habla → final: 1850–1957 ms.

**Volúmenes medidos** (percentil 90 por tramo, micrófono del entorno de
desarrollo):

| Audio | Nivel |
|---|---|
| Ruido de fondo | 0.001–0.004 |
| Voz baja al inicio de la sesión | 0.0086–0.0090 (un tramo en 0.0077) |
| Voz "pasito" | 0.017–0.022 |
| Voz normal o fuerte | 0.025–0.06 |

El umbral actual (0.008) queda apenas un 10 % por debajo de la voz más
baja medida y al doble del ruido: con poco margen hacia la voz.

---

## 7. Cortes en pausas y ruido que mantenía frases abiertas

**Fecha:** 2026-10-01
**Cambios:**
- Al llegar a `MAX_BUFFER_MS`, la frase se corta en la pausa más
  reciente de al menos `CUT_PAUSE_MIN_MS` (150 ms) en la segunda mitad
  del buffer, y el audio posterior sigue como la frase en curso. Si no
  hay pausa, se corta en el límite como antes.
- Un frame cuenta como habla solo si el VAD lo marca y su volumen
  alcanza `MIN_RMS`.

**Problema que se encontró al probar el primer cambio:** el VAD seguía
marcando el ruido de fondo (0.0016–0.0035) como habla después de que la
persona se callaba. El contador de silencio se reiniciaba, la frase no
se cerraba y el buffer se llenaba de ruido hasta el límite; con tan poca
voz en el buffer, el filtro del final la descartaba. Se perdieron varias
frases, entre ellas "Hola, ¿cómo estás?". En el cliente se veía como si
la frase siguiente "reemplazara" a la anterior.

| | Solo corte en pausas | Con filtro de energía por frame |
|---|---:|---:|
| Frases perdidas | varias | 0 de 9 |
| Frases mantenidas abiertas por ruido | 3 | 0 |
| Cortes en pausa / forzados | 5 / 3 | 2 / 0 |
| Fin de la habla → final | — | 1813–2099 ms |

**Observación:** en una frase se perdió un "No" inicial corto, probablemente
porque quedó fuera de la ventana de confirmación del inicio. Ocurrió una
vez; si se repite, se ajusta con `SPEECH_ONSET_WINDOW_MS`.

---

## 8. Perfil de tiempo real (rama `tiempo-real`)

**Fecha:** 2026-10-14
**GTX/RTX 4050**, Whisper `medium` (finales) + `small` (parciales), CUDA float16.

El objetivo dejó de ser "optimizar una espera" y pasó a ser que la app
firme **mientras la persona habla**. Eso cambió el perfil entero.

| | Antes | Ahora |
|---|---:|---:|
| Fin del habla → transcripción final | 1813 – 2099 ms | **p50 839 / p95 958 / max 1093 ms** |
| Inferencia de un parcial | 350 – 630 ms | **p50 92 / p95 172 ms** |
| Inferencia de un final | 246 – 332 ms | p50 249 / p95 338 ms |
| Espera por la GPU | picos de 4 – 8 s | **p50 0.4 / p95 0.8 ms** |
| Audio acumulado sin procesar | — | p50 −21 ms (sin acumulación) |
| Costo del VAD por frame | — | p50 0.07 ms |

Medido sobre los 28 fixtures de `scripts/make_audio_fixtures.py`,
reproducidos a velocidad real por el WebSocket del backend.

### Descomposición del fin del habla al final

| Término | Antes | Ahora | Por qué |
|---|---:|---:|---|
| Hangover de silencio | 1500 ms | **600 ms** | `SILENCE_TIMEOUT_MS`. El piso por debajo del cual una pausa natural entre palabras parte la frase. |
| Ventana de onset | 300 ms | **150 ms** | `SPEECH_ONSET_WINDOW_MS`. Son 150 ms en los que no existe siquiera un buffer del cual sacar un parcial. |
| Inferencia | 350 – 630 ms | **249 – 338 ms** | Whisper siempre paga su ventana de 30 s, complete o no el buffer. |

### `InferenceScheduler`

El descubrimiento que lo justificó: **CTranslate2 serializa igual** por
abajo (`num_workers=1`), así que sin un orden explícito el orden de
ejecución es el de llegada. Un parcial despachado 100 ms antes que un
final se ejecuta primero, y el usuario espera los dos. La entrada 4 de
este documento ya medía picos de 4 a 8 s.

La solución no es más concurrencia sino **orden**: una cola priorizada
donde `PRIORITY_FINAL` salta a los parciales que todavía no empezaron. El
parcial que ya está usando la GPU se deja terminar — cancelarlo a mitad
cuesta una GPU desperdiciada y no ahorra nada.

Resultado: `stt_partial_queue_ms` pasó de picos de 4 – 8 s a p95 0.8 ms.

### Dos modelos

| Modelo | Uso | Inferencia |
|---|---|---:|
| `small` | parciales | p50 92 ms |
| `medium` | finales | p50 249 ms |

Un parcial se tira a la basura en cuanto llega el final, así que no
necesita la precisión de `medium`. `PARTIAL_MODEL_SIZE` vacío (o igual a
`MODEL_SIZE`) deja un solo modelo, que es lo que se hacía antes y lo que
corresponde si la VRAM no alcanza para dos.

Precalentado: 96 ms el modelo de parciales, 488 ms el de finales. Se hace
en el arranque, en el hilo del lifespan, para que la primera frase real no
pague la compilación de kernels de CUDA.

### El filtro que se comía audio real

`AVG_LOGPROB_THRESHOLD` estaba en `-0.8`, más estricto que el default de
Whisper (`-1.0`). Medido sobre los fixtures, hacía que **"cómo estás" y
"estoy triste" salieran como transcripción vacía**, aunque el modelo las
había transcrito bien por `POST /transcribe`. Un final vacío es
indistinguible de un micrófono roto, que es exactamente la clase de
fallo que hace perder el tiempo de un agente.

Volvió a `-1.0`, y `transcribe_buffer` ahora devuelve `drop_reason` para
que un vacío se pueda explicar: `rms` (audio en silencio), `no_speech`
(el modelo lo considera silencio) o `logprob` (confianza baja).

Mirar `stt_dropped_total{reason="logprob"}` en `/metrics` es el guard: si
sube, el filtro se está comiendo audio transcrito correctamente.

### Timestamps por palabra

Cada parcial y final lleva ahora `words: [{w, s, e}]` y `audio_ms`. No
es un detalle del protocolo: es lo que le permite al backend decidir qué
palabra ya terminó de decirse en vez de tener que suponer "todo menos la
última".

Costo medido: despreciable. El alineamiento con atención cruzada ya está
en la pasada de decodificación.

### El buffer O(n²)

El buffer de habla concatenaba el frame de 30 ms contra el arreglo
completo en cada frame: para 8 s de utterances son ~270 concatenaciones de
un arreglo que llega a 128k muestras, unos 68 MB de memcpy, **todo en el
hilo del bucle de eventos**. Ese hilo es compartido por todas las
conexiones, así que una sesión saturada retrasaba la VAD de las demás.

Ahora acumula en una lista y junta una sola vez, al transcribir. El costo
medido del VAD por frame (0.07 ms) confirma que ya no hay trabajo pesado
en el bucle.

### Pendiente

- **Varias sesiones simultáneas.** Todo se midió con una. Con varias
  comparten la GPU y el scheduler las serializa, así que el tiempo por
  cliente sube linealmente. Con `medium` (1.5 GB de VRAM) entran bien
  varias en una GPU de 6 GB.
- **`PARTIAL_MODEL_SIZE=base` o `tiny`.** Aún más rápido, a costa de
  parciales bastante más ruidosos. No se midió.
- **Cancelar un parcial en vuelo.** Se hace cuando el utterance cierra,
  pero solo descarta el resultado: el hilo de la GPU sigue hasta que
  CTranslate2 termina, porque ahí no se puede interrumpir a mitad.
