#!/usr/bin/env bash
# Verificación local del STT: lo que antes corría en GitHub Actions.
#
#     tools/calidad/verificar.sh
#
# Compila `app/` y `scripts/` (lo único que se puede verificar sin GPU) y
# después escanea credenciales en el árbol y la historia. El escaneo vive en el
# repositorio hermano de la app, que es el que conoce los cinco repositorios:
#
#     ../signall-android/tools/calidad/leaks.sh
#
# Probar el STT de verdad necesita el modelo y una GPU; eso no corre en un
# runner de GitHub y tampoco acá sin GPU:
#
#     python -m scripts.verify_session

set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$RAIZ"

echo "== python -m compileall =="
python3 -m compileall -q app scripts

echo
echo "== gitleaks =="
LEAKS="../signall-android/tools/calidad/leaks.sh"
if [ -x "$LEAKS" ]; then
  "$LEAKS"
else
  echo "No está $LEAKS (el repo hermano de la app no está al lado)." >&2
  echo "El escaneo de credenciales queda sin hacer." >&2
  exit 2
fi
