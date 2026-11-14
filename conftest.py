"""El repo no es un paquete instalado: los tests importan `app` y `scripts`.

pytest agrega la raíz al `sys.path` cuando encuentra este archivo, así que
`import app` y `import scripts` funcionan sin depender del directorio desde el
que se invoque ni de un `pip install -e`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
