"""Barrera de contenido para lo que no es voz.

Por qué existe (2026-11-05): los filtros acústicos (`rms`, `avg_logprob`,
`no_speech_prob`) rechazan bien el ruido cuando se manda el archivo completo por
`POST /transcribe`, pero por el camino de uso —streaming con VAD— la tos y los
carraspeos llegan a texto. Medido en `PR-STT-03` el 2026-11-04: de los 8 audios
sin voz, 2 produjeron finales con texto («COUGH | Cof Cof | COUGH COUGH» para la
tos, «Grrrr | Ejem» para los carraspeos), mientras que el mismo archivo por
`POST` daba `drop_reason` en los 8.

La causa es que el buffer del streaming no es el del archivo: el VAD recorta el
silencio de los costados, el `rms` sube y la confianza del modelo con una tos
aislada pasa el umbral. Lo acústico ya no distingue; lo que sí distingue es el
contenido: Whisper escribe onomatopeyas y coletillas de subtítulos cuando no hay
habla.

Esto es una lista de patrones, no un modelo. Por eso cada patrón es específico:
`GRACIAS` es una glosa real del catálogo y no puede caer acá, `GRACIAS POR VER EL
VÍDEO` sí. La lista se valida contra los 8 audios de AU-03 en `PR-STT-03`; si
aparece una onomatopeya nueva, se agrega con su audio como evidencia.
"""

import re

# Cada patrón se aplica al texto ya normalizado por `_fold`, que lo deja en
# mayúsculas, sin acentos, sin puntuación y con espacios simples.
_PATRONES = [
    # Tos, carraspeo, garganta: lo que salió en PR-STT-03.
    r"^(COUGH|COF|COF COF|COUGH COUGH|EJEM|EJEM EJEM|GRRR+|RRR+|AH EM|AHEM)$",
    # Risa o suspiro aislado, sin palabras.
    r"^(JA|JA JA|JAJAJA+|JE JE|UH|UMM?|MMM+|EH|OH)$",
    # Coletillas de subtítulos que Whisper inventa sobre ruido o silencio.
    r"^(SUSCRIBETE AL CANAL|GRACIAS POR VER EL VIDEO|GRACIAS POR VER EL VIDEO!?"
    r"|SUBTITULOS REALIZADOS POR [A-Z ]+|SUBTITULOS POR [A-Z ]+)$",
    # Música o ruido descrito entre corchetes por el modelo.
    r"^\[?(MUSICA|MUSIC|RUIDO|NOISE|APLAUSOS|APPLAUSE|SILENCIO)\]?$",
]

_COMPILADOS = [re.compile(p) for p in _PATRONES]


def _fold(text: str) -> str:
    """Mayúsculas, sin acentos, sin puntuación y con espacios simples."""
    sin_acentos = text.translate(str.maketrans("ÁÉÍÓÚÜÑáéíóúüñ", "AEIOUUNaeiouun"))
    limpio = re.sub(r"[^\w\s\[\]]", " ", sin_acentos.upper())
    return " ".join(limpio.split())


def is_non_speech(text: str) -> bool:
    """True si el texto es una onomatopeya o una coletilla, no una frase.

    Un texto vacío no es "no voz": eso ya lo resuelven los filtros acústicos y
    su `drop_reason` es más informativo.
    """
    folded = _fold(text)
    if not folded:
        return False
    return any(patron.match(folded) for patron in _COMPILADOS)
