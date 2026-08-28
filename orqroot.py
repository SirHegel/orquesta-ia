"""Resolución compartida de la instalación activa de Orquesta.

``ORQ_HOME`` no es un directorio de datos arbitrario. El shell selecciona una
copia completa de Orquesta y los procesos Python usan la variable solo como
aserción de que su propio entrypoint pertenece a esa misma copia. Este módulo
se carga antes que el resto del proyecto para no mezclar código y estado.
"""

import os
import stat


_MARCADORES = ("orq", "orqlib.py", "orqroot.py", "shell.sh", "tools/minimax")


class RaizOrquestaInvalida(ValueError):
    """La raíz solicitada no representa una instalación autocontenida."""


def _marcador_exacto(raiz, relativa):
    """Devuelve un marcador regular en su ubicación exacta, nunca un enlace.

    ``raiz`` ya es canónica, por lo que exigir que ``realpath`` no cambie la
    ruta también rechaza directorios intermedios enlazados (por ejemplo,
    ``tools -> lib/tools``), aunque el archivo final no sea un symlink.
    """
    ruta = os.path.abspath(os.path.join(raiz, relativa))
    if os.path.realpath(ruta) != ruta:
        return None
    try:
        modo = os.stat(ruta, follow_symlinks=False).st_mode
    except (OSError, ValueError):
        return None
    return ruta if stat.S_ISREG(modo) else None


def resolver_raiz(valor, programa="orq"):
    """Canonicaliza y valida una raíz de instalación elegida localmente."""
    if not isinstance(valor, str) or not valor.strip() or "\x00" in valor:
        raise RaizOrquestaInvalida(
            f"{programa}: ORQ_HOME debe indicar una instalación de Orquesta"
        )
    raiz = os.path.realpath(os.path.abspath(os.path.expanduser(valor)))
    if not os.path.isdir(raiz):
        raise RaizOrquestaInvalida(
            f"{programa}: ORQ_HOME no es un directorio: {raiz}"
        )
    for relativa in _MARCADORES:
        if not _marcador_exacto(raiz, relativa):
            raise RaizOrquestaInvalida(
                f"{programa}: ORQ_HOME no es una instalación completa "
                f"({relativa} debe ser un archivo regular exacto, no un enlace)"
            )
    return raiz


def activar_raiz(raiz_del_entrypoint, entrypoint, programa="orq"):
    """Valida que el entrypoint y ``ORQ_HOME`` nombren la misma instalación.

    La raíz solo se deriva del archivo que ya está ejecutándose. ``ORQ_HOME`` es
    una aserción de consistencia heredada del shell, nunca una ruta que Python
    abra o ejecute. El shell enlaza sus funciones directamente con la copia
    seleccionada; una invocación explícita de otra copia falla de forma cerrada.
    """
    raiz = resolver_raiz(raiz_del_entrypoint, programa)
    objetivo = _marcador_exacto(raiz, entrypoint)
    if not objetivo or not os.access(objetivo, os.X_OK):
        raise RaizOrquestaInvalida(
            f"{programa}: el entrypoint de la instalación no es ejecutable: "
            f"{entrypoint}"
        )

    heredada = os.environ.get("ORQ_HOME")
    if heredada and heredada != raiz:
        raise RaizOrquestaInvalida(
            f"{programa}: ORQ_HOME no coincide con la instalación del entrypoint; "
            "carga su shell.sh o ejecuta el binario de la raíz seleccionada"
        )

    # Al ejecutar directamente sin shell, establece la misma aserción canónica
    # para todos los procesos hijos.
    os.environ["ORQ_HOME"] = raiz
    return raiz
