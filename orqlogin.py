#!/usr/bin/python3 -I
"""Autenticador interactivo lanzado con argv fijo desde el panel local."""

import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import orqroot

try:
    orqroot.activar_raiz(
        os.path.dirname(os.path.realpath(__file__)), "orqlogin.py", "orqlogin"
    )
except orqroot.RaizOrquestaInvalida as exc:
    raise SystemExit(str(exc))

import orqlib as L


def _entorno_login(pid, perfil):
    entorno = L.entorno(pid, perfil)
    home = L.home_de(pid, perfil)
    proveedor = perfil.get("provider")
    if proveedor == "claude":
        entorno["CLAUDE_CONFIG_DIR"] = home
    elif proveedor == "gpt":
        entorno["CODEX_HOME"] = home
    elif proveedor == "gemini":
        entorno["GEMINI_CLI_HOME"] = home
        entorno["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        if perfil.get("auth") == "oauth":
            entorno["GOOGLE_GENAI_USE_GCA"] = "true"
    navegador = perfil.get("navegador")
    if navegador:
        entorno["BROWSER"] = navegador
    return entorno


def main():
    pid = os.environ.pop("ORQ_LOGIN_PROFILE", "")
    if not L.id_perfil_valido(pid):
        raise SystemExit("perfil de login invalido")
    perfil = L.cfg().get("profiles", {}).get(pid)
    if not isinstance(perfil, dict):
        raise SystemExit("perfil de login inexistente")
    proveedor = perfil.get("provider")
    if proveedor not in ("claude", "gpt", "antigravity", "gemini", "minimax"):
        raise SystemExit("proveedor de login no permitido")
    if not L.navegador_valido(perfil.get("navegador")):
        raise SystemExit("navegador de login no permitido")

    print(f"\n  CONECTAR {pid}\n  {proveedor}\n", flush=True)
    if proveedor == "minimax":
        try:
            clave = getpass.getpass("  API key> ").strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\nlogin cancelado")
        try:
            L.guardar_api_key(pid, perfil, clave)
        except (ValueError, OSError):
            raise SystemExit("no pude guardar una clave valida de forma segura")
        if L.ruta_api_key(pid, perfil) is None:
            raise SystemExit("ruta privada de la clave invalida")
        with L.bloqueo():
            config = L.cfg()
            config["profiles"][pid]["auth"] = "apikey"
            config["profiles"][pid]["api_key_file"] = "api_key"
            L.guardar_cfg(config)
        print("\n  clave guardada con permisos 0600")
        return

    entorno = _entorno_login(pid, perfil)
    if proveedor == "claude":
        print("  Dentro de Claude escribe /login.\n", flush=True)
        os.execvpe("claude", ["claude"], entorno)
    if proveedor == "gpt":
        os.execvpe("codex", ["codex", "login"], entorno)
    if proveedor == "antigravity":
        os.execvpe("agy", ["agy"], entorno)
    os.execvpe("gemini", ["gemini"], entorno)


if __name__ == "__main__":
    try:
        main()
    except L.ErrorConfiguracion as exc:
        raise SystemExit(f"configuracion rechazada: {exc}")
    except OSError as exc:
        raise SystemExit(f"no pude iniciar el proveedor: {exc}")
