#!/usr/bin/env bash
# Cobertura real de `app/` con los dos scripts de verificación que corren sin GPU.
#
#     scripts/cobertura.sh
#
# Deja `coverage.xml` en la raíz del repo, que es el archivo que lee SonarQube
# (`sonar.python.coverage.reportPaths`). Los datos crudos (`.coverage*`) quedan
# al lado y están en `.gitignore`.
#
# Por qué corre en Docker y no en el host: los scripts importan `app/`, que
# importa `numpy`, `webrtcvad` y `faster-whisper`. La imagen del servicio ya los
# tiene, así que medir la cobertura es una corrida descartable y no una
# instalación de 2 GB en la máquina de quien la corre. La imagen no trae
# `coverage` ni `pytest` (no son dependencias del servicio), así que se instalan
# desde `requirements-dev.txt` dentro del contenedor de cada corrida.
#
# No necesita GPU: los dos scripts reemplazan el modelo y el VAD por dobles.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

rm -f .coverage .coverage.* coverage.xml

docker compose run --rm --no-deps \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e DUENO="$(id -u):$(id -g)" \
    -v "$RAIZ:/src" \
    -w /src \
    stt bash -lc '
        set -euo pipefail
        pip install -q --disable-pip-version-check -r requirements-dev.txt

        # `--parallel-mode` deja un archivo por corrida (`.coverage.XXXX`) y
        # `combine` los suma: así una falla de un script no pisa los datos del
        # otro, y mañana se pueden correr en paralelo de verdad.
        for script in scripts/verify_session.py scripts/verify_non_speech.py; do
            echo "== $script =="
            coverage run --parallel-mode "$script"
        done

        # La suite de pytest repite esos casos y suma los de `transcriber.py`,
        # que ningún script suelto ejercita.
        echo "== pytest tests/ =="
        coverage run --parallel-mode -m pytest -q tests

        coverage combine
        coverage report --show-missing
        coverage xml -o coverage.xml

        # El contenedor corre como root: los artefactos quedarían sin permiso
        # de escritura para quien los corre y la próxima corrida fallaría.
        chown "$DUENO" coverage.xml .coverage
        rm -rf .pytest_cache
    '

echo
echo "coverage.xml en $RAIZ/coverage.xml"
