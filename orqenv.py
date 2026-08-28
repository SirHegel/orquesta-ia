#!/usr/bin/python3 -I
"""Puente de entorno para Bash sin evaluar datos de ``profiles.json``.

La salida usa campos NUL-delimitados. ``shell.sh`` solo asigna nombres de una
lista fija con ``printf -v``; ningún valor de perfil se interpreta como shell.
"""

import os
import sys

_RAIZ_ENTRYPOINT = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _RAIZ_ENTRYPOINT)
import orqroot

try:
    orqroot.activar_raiz(_RAIZ_ENTRYPOINT, "orqenv.py", "orqenv")
except orqroot.RaizOrquestaInvalida as exc:
    raise SystemExit(str(exc))

import orqlib as L


PROVEEDORES = {"claude", "gpt", "gemini", "antigravity", "minimax"}


def _campo(valor, predeterminado="-", limite=4096):
    if valor in (None, ""):
        return predeterminado
    texto = str(valor)
    if "\x00" in texto or len(texto) > limite:
        raise ValueError("valor de perfil invalido")
    return texto


def _emitir(campos):
    salida = bytearray()
    for campo in campos:
        texto = _campo(campo)
        salida.extend(texto.encode("utf-8", "strict"))
        salida.append(0)
    sys.stdout.buffer.write(salida)


def _perfil(config, pid):
    if not L.id_perfil_valido(pid):
        raise ValueError("id de cuenta invalido")
    perfil = config.get("profiles", {}).get(pid)
    if not isinstance(perfil, dict) or perfil.get("provider") not in PROVEEDORES:
        raise ValueError("cuenta desconocida o proveedor invalido")
    return perfil


def _clave(pid, perfil):
    ruta = L.ruta_api_key(pid, perfil)
    if not ruta:
        return "-"
    clave = L._leer_texto(ruta, "").strip()
    return _campo(clave, "-", 16_384)


def perfil(pid):
    config = L.cfg()
    p = _perfil(config, pid)
    proveedor = p["provider"]
    base_url = L.base_url_minimax(p) if proveedor == "minimax" else "-"
    secreto = _clave(pid, p) if proveedor in ("gemini", "minimax") else "-"
    _emitir([
        proveedor,
        L.home_de(pid, p),
        p.get("auth") if p.get("auth") in ("oauth", "apikey") else "-",
        secreto,
        base_url,
        _campo(p.get("model"), "-", 256),
    ])


def activo():
    config = L.cfg()
    perfiles = config.get("profiles", {})
    activas = config.get("_activas", {})
    pares = []
    for proveedor, pid in sorted(activas.items()):
        if proveedor not in PROVEEDORES or not L.id_perfil_valido(pid):
            continue
        p = perfiles.get(pid)
        if (not isinstance(p, dict) or p.get("provider") != proveedor
                or not p.get("enabled", True)):
            continue
        try:
            if not L.autenticado(pid, p):
                continue
            home = L.home_de(pid, p)
        except (OSError, ValueError):
            continue
        # MiniMax no se exporta globalmente: secuestraría la cuenta Claude.
        if proveedor == "minimax":
            continue
        if proveedor == "claude":
            pares += ["CLAUDE_CONFIG_DIR", home]
        elif proveedor == "gpt":
            pares += ["CODEX_HOME", home]
        elif proveedor == "gemini":
            pares += ["GEMINI_CLI_HOME", home,
                      "GEMINI_CLI_TRUST_WORKSPACE", "true"]
            if p.get("auth") == "oauth":
                pares += ["GOOGLE_GENAI_USE_GCA", "true"]
            else:
                clave = _clave(pid, p)
                if clave != "-":
                    pares += ["GEMINI_API_KEY", clave]
        pares += [f"ORQ_{proveedor.upper()}_CUENTA", pid]
    _emitir(pares)


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "active":
        activo()
        return
    if len(sys.argv) == 3 and sys.argv[1] == "profile":
        perfil(sys.argv[2])
        return
    raise SystemExit("uso: orqenv.py active | profile <id>")


if __name__ == "__main__":
    try:
        main()
    except (L.ErrorConfiguracion, OSError, ValueError) as exc:
        raise SystemExit(f"orqenv: {exc}")
