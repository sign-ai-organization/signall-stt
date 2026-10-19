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

## 8. Vuelta al flujo por oraciones: qué de la latencia se queda y qué no

**Fecha:** 2026-10-15
**Plan:** `sign-ai-docs/auditoria-flujo-2026-10-15.md` §6.2

Entre el 13 y el 14 de octubre este servicio corrió con un perfil de tiempo
real: cinco defaults cambiados (silencio 600 ms, parciales cada 250 ms, ventana
de inicio 150 ms al 0.7, 120 ms de silencio final conservado), un segundo modelo
`small` para los parciales y los timestamps por palabra. La unidad de traducción
del backend pasó a ser el fragmento y el avatar firmaba mientras la persona
hablaba.

Ese camino no aparece en ningún diagrama del SDD ni en los requisitos, así que
se desarmó y la rama quedó en `flujo-oraciones`. La entrada 7 de esta bitácora
y las seis anteriores **no se tocan**: describen valores que siguen en pie.

### Lo que se conserva, y por qué no es del perfil de tiempo real

**El umbral de logprob, en `-1.0`.** A `-0.8` el filtro descartaba frases que el
modelo había transcrito bien: sobre los fixtures de
`scripts/make_audio_fixtures.py` se comía "cómo estás" y "estoy triste", que
llegaban como transcripción vacía sin dejar rastro. Descartar audio real es
peor que aceptar una frase dudosa. Es el default de faster-whisper y de
whisper mismo, y el valor de referencia para el filtro pasó a ser `no_speech`.

**El buffer con `list.append`.** Antes cada frame de 30 ms se concatenaba contra
el buffer completo: para 8 s de habla son ~270 concatenaciones de un arreglo que
llega a 128k muestras, ~68 MB de memcpy, en el hilo del bucle de eventos. Eso
bloqueaba la VAD de *todas* las conexiones, no solo la propia. Es un bug de
concurrencia (RNF-REL-01), no una decisión de latencia.

**El `InferenceScheduler`.** Sin orden explícito, el orden de llegada es el
orden de ejecución: un parcial que arrancó antes que un final se ejecutaba
primero. Con `MAX_CONCURRENT_INFERENCE=1` es un envoltorio sin efecto sobre el
tiempo, y su valor aparece cuando hay más de una sesión. Se conserva entero.

**El motivo del descarte y la métrica de atraso.** `TranscriptionResult` dice si
el final se descartó por `rms`, `no_speech`, `logprob` o `empty`, y `_lag_ms`
mide *audio recibido menos el que ya estaba en el buffer cuando arrancó la
inferencia*. Antes medía contra el reloj de la sesión, lo que mezclaba dos cosas
sin relación: con el micrófono apagado 20 s el número llegaba a 20000 ms y
parecía una GPU saturada, y al reproducir más rápido que tiempo real salía
negativo. Sin ese dato, un final vacío es indistinguible de un micrófono roto.

**El `end` que siempre responde.** Vacío incluido. Es lo que hace el botón del
micrófono de la app: sin respuesta, el cliente se come su propio timeout y
reporta como fallida una frase que nunca existió.

### Lo que se va

| | En el perfil | Ahora |
|---|---|---|
| `SILENCE_TIMEOUT_MS` | 600 ms | **1500 ms**, el del diagrama 6.3 |
| `PARTIAL_INTERVAL_MS` | 250 ms | 400 ms |
| `SPEECH_ONSET_WINDOW_MS` / `_RATIO` | 150 ms / 0.7 | 300 ms / 0.9 |
| `FINAL_TRAILING_SILENCE_KEEP_MS` | 120 ms | 300 ms |
| `MIN_AUDIO_MS_FOR_PARTIAL` | 160 ms | 200 ms |
| Modelos | dos (`medium` + `small`) | uno |
| `WORD_TIMESTAMPS` | `true` | `false` |

Los parciales **se quedan**: son RF-STT-02 y son del 30 de septiembre,
anteriores al perfil. El texto se muestra progresivo porque es un requisito, no
porque sirva para representar antes.

### Lo que hay que remedir antes de comparar

Las cifras del perfil de tiempo real (fin del habla → final **p50 839 ms /
p95 958 ms**, inferencia de un parcial **p50 92 ms**, espera por la GPU
**p95 0.8 ms**) **no son comparables** con las de ahora: el silencio era de 600
ms en vez de 1500, y 600 ms es exactamente el término que se quitó. Repetir la
medición con `SILENCE_TIMEOUT_MS=1500` y un solo modelo antes de sacar ninguna
conclusión. La entrada 7 sigue siendo la referencia de referencia: 1813–2099 ms
de fin del habla a final disponible.

con los valores de ahora, sobre los fixtures y por la pila completa, el final
sale entre 205 y 227 ms de inferencia y la espera por la GPU es de ~1 ms. Eso no
es una cifra de latencia de titular: le faltan los 1500 ms de silencio más lo
que el backend tarde en traducir. Para ese número hay que medir
`stt_end_to_final_ms` y `rt_first_sign_ms` juntos, que están en los dos servicios.
