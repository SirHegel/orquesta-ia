"""Resolución compartida de la instalación activa de Orquesta.

``ORQ_HOME`` no es un directorio de datos arbitrario: selecciona una copia
completa de Orquesta.  Los entrypoints usan este módulo antes de importar el
resto del proyecto para no mezclar el shell/estado de una instalación con el
código de otra.
"""

import os
import sys


_MARCADORES = ("orq", "orqlib.py", "orqroot.py", "shell.sh", "tools/minimax")


class RaizOrquestaInvalida(ValueError):
    """La raíz solicitada no representa una instalación autocontenida."""


def _ruta_interna(raiz, relativa):
    """Resuelve un marcador sin aceptar enlaces que escapen de ``raiz``."""
    ruta = os.path.realpath(os.path.join(raiz, relativa))
    try:
        if os.path.commonpath((raiz, ruta)) != raiz:
            return None
    except ValueError:
        return None
    return ruta


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
        ruta = _ruta_interna(raiz, relativa)
        if not ruta or not os.path.isfile(ruta):
            raise RaizOrquestaInvalida(
                f"{programa}: ORQ_HOME no es una instalación completa "
                f"(falta {relativa})"
            )
    return raiz


def activar_raiz(raiz_del_entrypoint, entrypoint, programa="orq"):
    """Activa una sola raíz y entrega la ejecución a su propio entrypoint.

    Cuando un shell de la copia A encuentra en ``PATH`` un enlace a la copia B,
    el ``ORQ_HOME`` exportado por el shell gana. El proceso se reemplaza por el
    entrypoint de A antes de cargar ``orqlib``; así no existe una combinación
    código-B/estado-A.
    """
    elegida = os.environ.get("ORQ_HOME") or raiz_del_entrypoint
    raiz = resolver_raiz(elegida, programa)
    objetivo = _ruta_interna(raiz, entrypoint)
    if not objetivo or not os.access(objetivo, os.X_OK):
        raise RaizOrquestaInvalida(
            f"{programa}: el entrypoint de ORQ_HOME no es ejecutable: {entrypoint}"
        )

    # ``sys.argv[0]`` pertenece al runner cuando el CLI se carga en pruebas o
    # como módulo. La ubicación del entrypoint que importó este helper es la
    # referencia estable tanto para ejecución directa como para symlinks.
    actual = os.path.realpath(os.path.join(raiz_del_entrypoint, entrypoint))
    entorno = dict(os.environ)
    entorno["ORQ_HOME"] = raiz
    if actual != objetivo:
        os.execve(objetivo, [objetivo, *sys.argv[1:]], entorno)

    # También normaliza ORQ_HOME al ejecutar directamente, para que todos los
    # procesos hijos hereden exactamente la misma raíz canónica.
    os.environ["ORQ_HOME"] = raiz
    return raiz
