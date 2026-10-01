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
