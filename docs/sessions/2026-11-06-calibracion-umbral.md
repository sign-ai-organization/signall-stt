# Calibración de `AVG_LOGPROB_THRESHOLD`: se queda en −1.0

- **Fecha:** 2026-11-06
- **Rama:** `fix/fases1-3`, desde `main` (`1208db8`)
- **Origen:** Fase 1 de `../sign-ai-android/docs/plan-pruebas/Plan de implementación 2 - Plan Prueba SignAll.md`, ítem `STT-03`.

---

## Qué llegó

El plan proponía endurecer `AVG_LOGPROB_THRESHOLD` de −1.0 a −0.5 para que la tos y
los carraspeos no llegaran a texto, midiendo en la misma corrida `PR-STT-03` (8 audios
sin voz) y `PR-STT-01` (WER de los 60 audios de voz real). El propio plan aclaraba que
esto **no** se cambia en el código: es un experimento de calibración.

## Qué se hizo

Un script dentro del contenedor (`/tmp/calibrar_umbral.py`) cargó el modelo `medium` en
la GPU y transcribió los 60 audios de `AU-01` y los 8 de `AU-03` con dos valores del
umbral, usando el mismo manifiesto (`audios/manifiesto.csv`, columna `texto_del_plan`)
como referencia del WER. No se tocó código ni la configuración del servicio.

## Qué se midió

Modelo `medium`, `float16`, GPU RTX 4050, audios WAV mono 16 kHz de
`../sign-ai-android/docs/plan-pruebas/audios/16k/`, 2026-11-06.

| `AVG_LOGPROB_THRESHOLD` | WER AU-01 | errores/palabras | AU-01 con final vacío | AU-03 con texto |
|---|---|---|---|---|
| **−1.0 (actual)** | **1,77 %** | 5/282 | 1 de 60 | 0 de 8 |
| −0.5 (propuesto) | 21,63 % | 61/282 | 17 de 60 | 0 de 8 |

El umbral −0.5 no compra nada en `PR-STT-03`: con −1.0 los 8 audios sin voz ya dan
final vacío, porque desde el 2026-11-05 existe la barrera de contenido
(`app/non_speech.py`) además de los filtros acústicos. Lo que sí rompe es `PR-STT-01`:
17 audios de voz real quedan sin transcripción y el WER sube más de veinte puntos.

## Qué se decidió

**Dejar `AVG_LOGPROB_THRESHOLD` en −1.0.** Es el default de Whisper, es el valor que ya
tenía el repo desde el 2026-10-15 (la sesión
`2026-10-15-vuelta-al-flujo-por-oraciones.md` documenta que −0.8 ya descartaba frases
válidas) y −0.5 es peor por donde se lo mire.

## Qué quedó pendiente

- Nada en este repo: el ítem de la Fase 1 se cierra sin cambio de código.
- La corrida no se commiteó como script porque es un experimento de calibración; los
  números quedan acá con su origen.
