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
    # GRR+ y no GRRR+: el parcial «¡Grr!» de AU-03-006 (PR-STT-03, 2026-11-06)
    # tiene dos erres y se escapaba.
    r"^(COUGH|COF|COF COF|COUGH COUGH|EJEM|EJEM EJEM|GRR+|RRR+|AH EM|AHEM)$",
    # Risa o suspiro aislado, sin palabras.
    r"^(JA|JA JA|JAJAJA+|JE JE|UH|UMM?|MMM+|EH|OH)$",
    # Coletillas de subtítulos que Whisper inventa sobre ruido o silencio.
    r"^(SUSCRIBETE AL CANAL|GRACIAS POR VER EL VIDEO|GRACIAS POR VER EL VIDEO!?"
    r"|SUBTITULOS REALIZADOS POR [A-Z ]+|SUBTITULOS POR [A-Z ]+)$",
    # Otra coletilla de subtítulos de Whisper: «Este es el canal de subtítulos en
    # español de la Iglesia de Jesucristo de los Santos de los Últimos Días…».
    # Sin cola fija a propósito: el modelo sigue inventando después. Medida en
    # PR-STT-03 el 2026-11-10 (4 de 160 corridas por POST, en AU-03-003 y 004).
    r"^(ESTE|ESTO) ES EL CANAL DE SUBTITULOS EN ESPANOL",
    # Créditos de subtítulos («Subtitulado por la comunidad de Amara.org»),
    # medidos en PR-STT-03 el 2026-11-10 sobre el ruido de AU-03-004.
    r"^SUBTITULAD[OA]S? POR",
    # Música o ruido descrito entre corchetes por el modelo.
    r"^\[?(MUSICA|MUSIC|RUIDO|NOISE|APLAUSOS|APPLAUSE|SILENCIO)\]?$",
]

_COMPILADOS = [re.compile(p) for p in _PATRONES]

# Direcciones web: nadie le dicta una URL al avatar. A diferencia de los patrones
# de arriba, estos valen en cualquier parte del texto («Más información en
# www.alimmenta.com», PR-STT-03, 2026-11-10). `_fold` ya cambió el punto por un
# espacio, así que «.com» llega como «COM».
_EN_CUALQUIER_PARTE = [re.compile(p) for p in (
    r"\bWWW\b",
    r"\bHTTPS?\b",
    r"\bAMARA ORG\b",
    # Variantes de las coletillas de arriba que el modelo escribe con otras
    # palabras alrededor («La música es el canal de subtítulos…», «¡Suscríbete
    # al canal y dale a like!»), medidas en PR-STT-03 el 2026-11-10.
    r"\bCANAL DE SUBTITULOS\b",
    r"\bSUSCRIBETE\b",
    r"\bDALE A (LIKE|LA CAMPANITA)\b",
)]


def _fold(text: str) -> str:
    """Mayúsculas, sin acentos, sin puntuación y con espacios simples."""
    sin_acentos = text.translate(str.maketrans("ÁÉÍÓÚÜÑáéíóúüñ", "AEIOUUNaeiouun"))
    limpio = re.sub(r"[^\w\s\[\]]", " ", sin_acentos.upper())
    return " ".join(limpio.split())


def is_non_speech(text: str) -> bool:
    """True si el texto es una onomatopeya o una coletilla, no una frase.

    Un texto vacío no es "no voz": eso ya lo resuelven los filtros acústicos y
    su `drop_reason` es más informativo. Un texto que solo trae puntuación
    (`. . . . . . .`, que Whisper escribió sobre el ruido de AU-03-004) sí lo
    es: no tiene ninguna letra ni número que traducir.
    """
    if not text.strip():
        return False
    folded = _fold(text)
    if not re.search(r"[A-Z0-9]", folded):
        return True
    if any(patron.search(folded) for patron in _EN_CUALQUIER_PARTE):
        return True
    return any(patron.match(folded) for patron in _COMPILADOS)
