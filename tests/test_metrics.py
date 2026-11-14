"""Pruebas de `app/metrics.py`: contadores, gauges, histogramas y su render.

No hay servicio ni GPU de por medio: esto es aritmética y formato. Lo que se
fija es lo que leen el `/metrics` de Prometheus y `/metrics.json`, que hasta
ahora no tenía ninguna prueba y por eso un cambio de formato rompía el
dashboards sin que nadie se enterara.
"""

from app.metrics import (
    _MAX_SAMPLES,
    ConcurrencyGauge,
    Histogram,
    MetricsRegistry,
    Timer,
    metrics,
    metrics_timer,
)


def registro_con_datos() -> MetricsRegistry:
    """Un registro chico con un contador, un gauge y un histograma."""
    registry = MetricsRegistry()
    registry.counter("stt_dropped_total", labels={"reason": "rms"})
    registry.counter("stt_dropped_total", labels={"reason": "rms"})
    registry.counter("stt_dropped_total", labels={"reason": "logprob"})
    registry.gauge("stt_sessions_active", 2)
    registry.histogram("stt_inference_ms", 40.0, "Inferencia por buffer", {"model": "medium"})
    return registry


# ── Histogram ──────────────────────────────────────────────────────────────────


def test_histograma_vacio_no_tiene_percentiles() -> None:
    histograma = Histogram(name="h", help="h")

    assert histograma.percentile(50) == 0.0
    assert histograma.snapshot() == {
        "count": 0,
        "sum": 0.0,
        "min": 0.0,
        "max": 0.0,
        "avg": 0.0,
        "p50": 0.0,
        "p95": 0.0,
        "p99": 0.0,
    }


def test_histograma_resume_min_max_y_promedio() -> None:
    histograma = Histogram(name="h", help="h")
    histograma.observe(10.0)
    histograma.observe(30.0)

    resumen = histograma.snapshot()

    assert resumen["count"] == 2
    assert resumen["sum"] == 40.0
    assert resumen["min"] == 10.0
    assert resumen["max"] == 30.0
    assert resumen["avg"] == 20.0


def test_los_percentiles_salen_de_las_muestras_ordenadas() -> None:
    histograma = Histogram(name="h", help="h")
    for valor in (40.0, 10.0, 30.0, 20.0):
        histograma.observe(valor)

    assert histograma.percentile(50) == 30.0
    assert histograma.percentile(95) == 40.0
    assert histograma.percentile(99) == 40.0


def test_la_muestra_circular_no_crece_sin_limite() -> None:
    histograma = Histogram(name="h", help="h")
    for valor in range(_MAX_SAMPLES):
        histograma.observe(float(valor))

    assert len(histograma.samples) == _MAX_SAMPLES
    assert histograma.count == _MAX_SAMPLES

    # La muestra que sigue pisa la más vieja y mueve el cursor.
    histograma.observe(9999.0)
    assert len(histograma.samples) == _MAX_SAMPLES
    assert histograma.samples[0] == 9999.0
    assert histograma.count == _MAX_SAMPLES + 1

    histograma.observe(8888.0)
    assert histograma.samples[1] == 8888.0


# ── MetricsRegistry ───────────────────────────────────────────────────────────


def test_contadores_y_gauges_viven_en_mapas_separados() -> None:
    """Leer un gauge con `counter_value` (o al revés) responde 0.

    Es la falla que documenta `gauge_value`: un gauge hecho como
    `gauge(name, counter_value(name) + 1)` decía siempre 1.
    """
    registry = registro_con_datos()

    assert registry.counter_value("stt_dropped_total", {"reason": "rms"}) == 2
    assert registry.gauge_value("stt_sessions_active") == 2
    assert registry.counter_value("stt_sessions_active") == 0.0
    assert registry.gauge_value("stt_dropped_total") == 0.0


def test_las_etiquetas_se_ordenan_para_que_la_clave_sea_la_misma() -> None:
    registry = MetricsRegistry()
    registry.counter("c", labels={"b": "2", "a": "1"})
    registry.counter("c", labels={"a": "1", "b": "2"})

    assert registry.counter_value("c", {"a": "1", "b": "2"}) == 2


def test_histograma_de_una_metrica_inexistente_es_vacio() -> None:
    assert MetricsRegistry().histogram_value("no_existe") == {}


def test_el_snapshot_json_trae_todo() -> None:
    snapshot = registro_con_datos().snapshot()

    assert snapshot["counters"] == {
        'stt_dropped_total{reason="logprob"}': 1.0,
        'stt_dropped_total{reason="rms"}': 2.0,
    }
    assert snapshot["gauges"] == {"stt_sessions_active": 2}
    assert snapshot["histograms"]['stt_inference_ms{model="medium"}']["count"] == 1
    assert isinstance(snapshot["uptime_s"], float)


# ── Render de Prometheus ──────────────────────────────────────────────────────


def test_el_render_declara_help_y_type_una_sola_vez() -> None:
    lineas = registro_con_datos().render().splitlines()

    assert lineas.count("# HELP stt_dropped_total stt_dropped_total") == 1
    assert lineas.count("# TYPE stt_dropped_total counter") == 1
    assert "# HELP stt_sessions_active stt_sessions_active" in lineas
    assert "# TYPE stt_sessions_active gauge" in lineas
    assert "# HELP stt_inference_ms Inferencia por buffer" in lineas
    assert "# TYPE stt_inference_ms histogram" in lineas


def test_el_render_de_contadores_y_gauges() -> None:
    lineas = registro_con_datos().render().splitlines()

    assert 'stt_dropped_total{reason="logprob"} 1' in lineas
    assert 'stt_dropped_total{reason="rms"} 2' in lineas
    assert "stt_sessions_active 2" in lineas


def test_el_render_de_buckets_es_acumulativo() -> None:
    lineas = registro_con_datos().render().splitlines()

    assert 'stt_inference_ms_bucket{le="5",model="medium"} 0' in lineas
    assert 'stt_inference_ms_bucket{le="50",model="medium"} 1' in lineas
    assert 'stt_inference_ms_bucket{le="+Inf",model="medium"} 1' in lineas


def test_el_render_de_sufijos_del_histograma() -> None:
    lineas = registro_con_datos().render().splitlines()

    for sufijo, valor in (
        ("_sum", "40"),
        ("_count", "1"),
        ("_min", "40"),
        ("_max", "40"),
        ("_p50", "40"),
        ("_p95", "40"),
        ("_p99", "40"),
    ):
        assert f'stt_inference_ms{sufijo}{{model="medium"}} {valor}' in lineas


def test_una_metrica_sin_help_usa_su_nombre() -> None:
    registry = MetricsRegistry()
    registry.histogram("sin_ayuda", 1.0)

    assert "# HELP sin_ayuda sin_ayuda" in registry.render().splitlines()


def test_el_render_termina_en_salto_de_linea_y_vacio_es_solo_eso() -> None:
    assert MetricsRegistry().render() == "\n"
    assert registro_con_datos().render().endswith("\n")


def test_los_numeros_enteros_salen_sin_decimales() -> None:
    registry = MetricsRegistry()
    registry.gauge("g", 3.0)
    registry.gauge("decimal", 1.5)
    registry.gauge("redondeado", 1 / 3)

    lineas = registry.render().splitlines()

    assert "g 3" in lineas
    assert "decimal 1.5" in lineas
    assert "redondeado 0.333" in lineas


# ── Timer y ConcurrencyGauge ──────────────────────────────────────────────────


def test_metrics_timer_publica_el_tiempo_del_bloque() -> None:
    timer = metrics_timer("prueba_timer", "Tiempo de prueba")

    with timer as abierto:
        assert abierto is timer

    assert timer.elapsed_ms >= 0.0
    assert metrics.histogram_value("prueba_timer")["count"] >= 1


def test_el_timer_publica_aunque_el_bloque_falle() -> None:
    try:
        with Timer("prueba_timer_error", "Tiempo de prueba") as timer:
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    assert timer.elapsed_ms >= 0.0
    assert metrics.histogram_value("prueba_timer_error")["count"] == 1


def test_el_concurrency_gauge_sube_y_baja() -> None:
    nombre = "prueba_activos"

    with ConcurrencyGauge(nombre, "En vuelo"):
        assert metrics.gauge_value(nombre) == 1
        with ConcurrencyGauge(nombre, "En vuelo"):
            assert metrics.gauge_value(nombre) == 2
        assert metrics.gauge_value(nombre) == 1

    assert metrics.gauge_value(nombre) == 0


def test_el_concurrency_gauge_no_baja_de_cero() -> None:
    nombre = "prueba_activos_bajo"

    ConcurrencyGauge(nombre, "En vuelo").__exit__(None, None, None)

    assert metrics.gauge_value(nombre) == 0
