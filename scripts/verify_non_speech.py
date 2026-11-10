"""Verifica la barrera de contenido de `app/non_speech.py` (PR-STT-03).

Dos cosas, y la segunda importa más que la primera:

1. Lo que Whisper inventa sobre tos, carraspeos y ruido se descarta. Los textos
   salen de las corridas reales de PR-STT-03 (2026-11-06 y 2026-11-10).
2. Lo que dice una persona no se descarta. Un filtro de contenido que se come
   «gracias» o «hola» rompe el producto en silencio.

No necesita modelo ni GPU. Se corre con

    python scripts/verify_non_speech.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.non_speech import is_non_speech  # noqa: E402

DEBE_DESCARTARSE = [
    "COUGH",
    "Cof Cof",
    "Ejem",
    "Grrrr",
    "¡Grr!",  # parcial de AU-03-006: dos erres
    "Grr",
    "Música",
    "[RUIDO]",
    "Suscríbete al canal",
    "Gracias por ver el video",
    ". . . . . . .",  # AU-03-004, POST
    "Más información en www.alimmenta.com",  # AU-03-004, POST
    "Subtitulado por la comunidad de Amara.org",  # AU-03-004, POST
    "Subtítulos por Pedro",
    "La música es el canal de subtítulos en español de la Iglesia de Jesucristo.",
    "Nuestro canal de subtítulos en español es el canal de subtítulos en español.",
    "¡Suscríbete al canal y dale a like!",
    "¡Suscríbete al canal y dale a la campanita!",
    "...",
    "Este es el canal de subtítulos en español de la Iglesia de Jesucristo de los Santos de los Últimos Días.",
    "Esto es el canal de subtítulos en español de la Iglesia de Jesucristo de los Santos de los Últimos Días.",
]

NO_DEBE_DESCARTARSE = [
    "",  # vacío: lo resuelven los filtros acústicos
    "   ",
    "Gracias",
    "Hola",
    "Hola, buenos días.",
    "¿Cómo estás?",
    "Quiero hablar con mi mamá",
    "Tengo hambre",
    "Este es mi hermano",
    "Esto es mío",
    "El canal está cerrado",
    "Quiero más información",
    "La comunidad es grande",
    "No",
    "3",
]

failures: list[str] = []


def check(texto: str, esperado: bool) -> None:
    obtenido = is_non_speech(texto)
    ok = obtenido == esperado
    print(f"  {'PASS' if ok else 'FAIL'}  {texto[:60]!r} -> {obtenido}")
    if not ok:
        failures.append(texto)


print("debe descartarse")
for texto in DEBE_DESCARTARSE:
    check(texto, True)

print("\nno debe descartarse")
for texto in NO_DEBE_DESCARTARSE:
    check(texto, False)

if failures:
    print(f"\n{len(failures)} comprobación(es) fallaron")
    sys.exit(1)
print("\nTodas las comprobaciones pasaron")
