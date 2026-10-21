"""Registro de metricas en texto de Prometheus, sin dependencias.

El servicio no tiene ni Prometheus ni OpenTelemetry, y para este proyecto no
hacen falta: lo que hay que poder responder es concreto y de bajo volumen.

- *Cuanto tarda* cada etapa (VAD, inferencia parcial, inferencia final) y
  donde se va el tiempo cuando algo va mal.
- *Que tan seguido* cae cada resultado: parciales, finales, descartes por RMS,
  descartes por logprob, transcripciones concurrentes.
- *Que tan grande* es la cola de audio que se acumula mientras la GPU
  transccribe, que es el sintoma clasico de saturacion.

Se expone en `GET /metrics` en el formato de exposicion de Prometheus
(`# HELP` / `# TYPE` / `nombre{label="valor"} valor`) para que se pueda
rasparnar con `curl`, meter en un `node_exporter`, o pegarlo en un dashboard
sin cambiar nada del codigo.

Los histogramas guardan solo conteo, suma, min, max y una lista de muestras
reservadas para el calculo de percentiles. La lista tiene tope
(`_MAX_SAMPLES`): un servicio de streaming produce miles de parciales por hora
y retenerlos todos seria una fuga de memoria disfrazada de instrumentacion.
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

_MAX_SAMPLES = 512


@dataclass
class Histogram:
    """Metrica de distribucion con percentiles aproximados.

    Percentiles exactos exigirian retener todas las muestras para siempre. Con
    una muestra circular de los ultimos `_MAX_SAMPLES` valores el p95 reciente
    es preciso — que es el que importa para notar una regresion — y la memoria
    queda acotada.
    """

    name: str
    help: str
    buckets: tuple[float, ...] = ()
    count: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    samples: list[float] = field(default_factory=list)
    _cursor: int = 0

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)

        if len(self.samples) < _MAX_SAMPLES:
            self.samples.append(value)
        else:
            self.samples[self._cursor] = value
            self._cursor = (self._cursor + 1) % _MAX_SAMPLES

    def percentile(self, pct: float) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        index = min(len(ordered) - 1, max(0, round((pct / 100) * (len(ordered) - 1))))
        return ordered[index]

    def snapshot(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "sum": round(self.total, 3),
            "min": round(self.minimum, 3) if self.minimum is not None else 0.0,
            "max": round(self.maximum, 3) if self.maximum is not None else 0.0,
            "avg": round(self.total / self.count, 3) if self.count else 0.0,
            "p50": round(self.percentile(50), 3),
            "p95": round(self.percentile(95), 3),
            "p99": round(self.percentile(99), 3),
        }


class MetricsRegistry:
    """Contadores, gauges e histogramas, seguros desde varios hilos.

    Whisper corre en `asyncio.to_thread`, o sea en hilos del pool por defecto,
    mientras el bucle de eventos sigue observando. `threading.Lock` cubre
    ambos: el GIL ya no alcanza como garantia porque las operaciones son
    varias y no atomicas.
    """

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], Histogram] = {}
        self._help: dict[str, str] = {}
        self._started_at = time.time()

    def _key(self, name: str, labels: dict[str, str] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted((labels or {}).items()))

    def counter(self, name: str, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += value

    def gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._gauges[self._key(name, labels)] = value

    def histogram(self, name: str, value: float, help_text: str = "", labels: dict[str, str] | None = None) -> None:
        key = self._key(name, labels)
        with self._lock:
            if key not in self._histograms:
                self._histograms[key] = Histogram(
                    name=name,
                    help=help_text or name,
                    buckets=(5, 10, 25, 50, 100, 200, 300, 500, 800, 1200, 2000, 4000, 8000),
                )
                if help_text:
                    self._help[name] = help_text
            self._histograms[key].observe(value)

    def counter_value(self, name: str, labels: dict[str, str] | None = None) -> float:
        with self._lock:
            return self._counters.get(self._key(name, labels), 0.0)

    def gauge_value(self, name: str, labels: dict[str, str] | None = None) -> float:
        """Valor actual de un gauge.

        Contadores y gauges viven en mapas separados, así que leer uno con
        `counter_value` responde siempre 0. Eso hacia que todo gauge hecho como
        `gauge(name, counter_value(name) + 1)` —`stt_sessions_active` y
        `ConcurrencyGauge`— dijera 1 hagas cuántas sesiones o inferencias
        hubiera: nunca veía su propio valor anterior. Es la clase de falla que
        no se nota, porque "1" es un número plausible.
        """
        with self._lock:
            return self._gauges.get(self._key(name, labels), 0.0)

    def histogram_value(self, name: str, labels: dict[str, str] | None = None) -> dict[str, Any]:
        with self._lock:
            histogram = self._histograms.get(self._key(name, labels))
            return histogram.snapshot() if histogram else {}

    def snapshot(self) -> dict[str, Any]:
        """Todo en un diccionario, para `/metrics?format=json` y para tests."""
        with self._lock:
            return {
                "uptime_s": round(time.time() - self._started_at, 1),
                "counters": {_render_key(k): v for k, v in sorted(self._counters.items())},
                "gauges": {_render_key(k): v for k, v in sorted(self._gauges.items())},
                "histograms": {
                    _render_key(k): h.snapshot() for k, h in sorted(self._histograms.items())
                },
            }

    def render(self) -> str:
        """Formato de exposicion de Prometheus."""
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            histograms = dict(self._histograms)

        lines: list[str] = []
        declared: set[str] = set()

        def declare(name: str, metric_type: str) -> None:
            if name not in declared:
                lines.append(f"# HELP {name} {self._help.get(name, name)}")
                lines.append(f"# TYPE {name} {metric_type}")
                declared.add(name)

        for (name, labels), value in sorted(counters.items()):
            declare(name, "counter")
            lines.append(f"{_render_name(name, labels)} {_num(value)}")

        for (name, labels), value in sorted(gauges.items()):
            declare(name, "gauge")
            lines.append(f"{_render_name(name, labels)} {_num(value)}")

        for (name, labels), histogram in sorted(histograms.items()):
            declare(name, "histogram")
            snapshot = histogram.snapshot()

            cumulative = 0
            for edge in histogram.buckets:
                cumulative = sum(1 for sample in histogram.samples if sample <= edge)
                bucket_labels = dict(labels) | {"le": str(edge)}
                lines.append(f"{_render_name(name + '_bucket', tuple(sorted(bucket_labels.items())))} {cumulative}")
            inf_labels = dict(labels) | {"le": "+Inf"}
            lines.append(f"{_render_name(name + '_bucket', tuple(sorted(inf_labels.items())))} {histogram.count}")

            for suffix, key in _HISTOGRAM_SUFFIXES:
                lines.append(f"{_render_name(name + suffix, labels)} {_num(snapshot[key])}")

        return "\n".join(lines) + "\n"


_HISTOGRAM_SUFFIXES = (
    ("_sum", "sum"),
    ("_count", "count"),
    ("_min", "min"),
    ("_max", "max"),
    ("_p50", "p50"),
    ("_p95", "p95"),
    ("_p99", "p99"),
)


def _render_name(name: str, labels: tuple[tuple[str, str], ...]) -> str:
    parts = [f'{key}="{value}"' for key, value in labels]
    return f"{name}{{{','.join(parts)}}}" if parts else name


def _render_key(key: tuple[str, tuple[tuple[str, str], ...]]) -> str:
    name, labels = key
    return _render_name(name, labels)


def _num(value: float) -> str:
    if isinstance(value, int) or value.is_integer():
        return str(int(value))
    return repr(round(value, 3))


metrics = MetricsRegistry()


class Timer:
    """Cronometra un bloque y lo publica como histograma.

        with metrics_timer("stt_frame", "vad_ms"):
            vad_speech = self._vad.is_speech(...)

    El patron se usa en los tres puntos calientes (VAD por frame, inferencia
    parcial, inferencia final) para no repetir el try/finally a mano.
    """

    def __init__(self, name: str, help_text: str, labels: dict[str, str] | None = None) -> None:
        self.name = name
        self.help = help_text
        self.labels = labels
        self.elapsed_ms = 0.0

    def __enter__(self) -> "Timer":
        self._started = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed_ms = (time.perf_counter() - self._started) * 1000
        metrics.histogram(self.name, self.elapsed_ms, self.help, self.labels)


def metrics_timer(name: str, help_text: str, labels: dict[str, str] | None = None) -> Timer:
    return Timer(name, help_text, labels)


class ConcurrencyGauge:
    """Cuenta sesiones/threads concurrentes de una clase de trabajo.

    Sube al entrar y baja al salir con `try/finally`, de modo que un
    `CancelledError` a mitad de una transcripcion no deje el contador colgado en
    un numero que jamas volveria a cero.

    Sube con `gauge_value` y no con `counter_value`: son mapas separados, y leer
    el contador daba 0 en cada entrada, asi que el gauge decia siempre 1 — con
    seis inferencias simultaneas en vuelo seguia diciendo 1.
    """

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help = help_text

    def __enter__(self) -> "ConcurrencyGauge":
        metrics.gauge(self.name, metrics.gauge_value(self.name) + 1)
        return self

    def __exit__(self, *exc: object) -> None:
        metrics.gauge(self.name, max(0.0, metrics.gauge_value(self.name) - 1))