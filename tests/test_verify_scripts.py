"""Los dos scripts de verificación, corridos bajo pytest.

`scripts/cobertura.sh` los ejecuta directo, con `coverage run`, y es el camino
que produce `coverage.xml`. Esto existe para que `pytest` (o
`pytest --cov=app`) mida lo mismo sin tener que recordar el comando de cada
script, y para que un fallo se reporte con el nombre de la prueba.

No necesitan GPU: `verify_session` reemplaza el modelo y el VAD por dobles, y
`verify_non_speech` solo ejercita la barrera de contenido.
"""

import asyncio

import pytest

from scripts.verify_non_speech import main as verificar_barrera_de_contenido
from scripts.verify_session import PRUEBAS


@pytest.mark.parametrize(
    "prueba",
    PRUEBAS,
    ids=[prueba.__name__.removeprefix("test_") for prueba in PRUEBAS],
)
def test_verify_session(prueba) -> None:
    asyncio.run(prueba())


def test_verify_non_speech() -> None:
    assert verificar_barrera_de_contenido() == 0
