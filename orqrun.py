#!/usr/bin/python3 -I
"""Supervisor interno de procesos: contiene y recolecta hijos desacoplados.

El proceso objetivo puede crear otra sesion o terminar antes que sus hijos. En
Linux este supervisor se convierte en *child subreaper*, de modo que esos
huérfanos se le reasignan y siguen dentro de un árbol que Orquesta puede
terminar de forma determinista. No se usa como interfaz pública.
"""

import ctypes
import os
import pwd
import re
import signal
import stat
import subprocess
import sys
import time


PR_SET_CHILD_SUBREAPER = 36
_senal_pendiente = 0
TEMP_ROOT = os.path.realpath("/tmp")
_CONFIG_GIT_EJECUTABLE = (
    r"^(include(if\..*)?\.path|filter\..*\.(clean|smudge|process)|"
    r"diff\..*\.(command|textconv)|merge\..*\.driver|"
    r"core\.(sshcommand|gitproxy|alternaterefscommand|worktree)|"
    r"extensions\.worktreeconfig|credential(\..*)?\.helper|"
    r"gpg(\..*)?\.program|remote\..*\.(uploadpack|receivepack|vcs)|"
    r"url\..*\.(insteadof|pushinsteadof))$"
)
_GIT_INTERNO = [
    "--no-pager", "--no-replace-objects",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.untrackedCache=false",
    "-c", "core.commitGraph=false",
    "-c", "fetch.writeCommitGraph=false",
    "-c", "core.attributesFile=/dev/null",
    "-c", "credential.helper=",
    "-c", "core.askPass=",
    "-c", "core.sshCommand=/usr/bin/ssh",
    "-c", "ssh.variant=ssh",
    "-c", "core.gitProxy=",
    "-c", "protocol.ext.allow=never",
    "-c", "diff.external=",
    "-c", "advice.graftFileDeprecated=false",
    "-c", "commit.gpgSign=false",
    "-c", "remote.origin.uploadpack=git-upload-pack",
    "-c", "remote.origin.receivepack=git-receive-pack",
]
_GIT_VERIFICACION = [
    "--no-pager", "--no-replace-objects",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.untrackedCache=false",
    "-c", "core.commitGraph=false",
    "-c", "advice.graftFileDeprecated=false",
]


def _activar_subreaper():
    if not sys.platform.startswith("linux"):
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    return libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0


def _binario_fijo(nombre, proveedor=False):
    """Resuelve solo raíces de ejecutables administradas, nunca PATH heredado."""
    candidatos = []
    if proveedor:
        home = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
        candidatos.append(os.path.join(home, ".local", "bin", nombre))
    candidatos.extend(
        os.path.join(raiz, nombre) for raiz in ("/usr/local/bin", "/usr/bin", "/bin")
    )
    for candidato in candidatos:
        try:
            real = os.path.realpath(candidato)
            estado = os.stat(real)
        except OSError:
            continue
        if (stat.S_ISREG(estado.st_mode) and os.access(real, os.X_OK)
                and estado.st_uid in (0, os.getuid())
                and not estado.st_mode & 0o002):
            return real
    return None


def _valor_opcion(valor):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+\[\]-]{0,159}", valor))


def _opciones_proveedor(modo, opciones):
    if len(opciones) < 2:
        return False
    if modo in ("claude", "codex"):
        if opciones[-2] != "--" or "--" in opciones[:-2]:
            return False
        prefijo = opciones[:-2]
    else:
        if opciones[-2] != "-p" or "-p" in opciones[:-2]:
            return False
        prefijo = opciones[:-2]

    if modo == "claude":
        if prefijo[:3] != ["-p", "--output-format", "json"]:
            return False
        resto = prefijo[3:]
        sin_valor = {"--dangerously-skip-permissions", "--chrome"}
        con_valor = {
            "--resume", "--session-id", "--model", "--effort",
            "--permission-mode",
        }
    elif modo == "codex":
        if prefijo[:2] != ["exec", "--skip-git-repo-check"]:
            return False
        resto = prefijo[2:]
        sin_valor = {"--dangerously-bypass-approvals-and-sandbox"}
        con_valor = {"--sandbox", "-m", "-c"}
    elif modo == "agy":
        if prefijo[:2] != ["--output-format", "json"]:
            return False
        resto = prefijo[2:]
        sin_valor = {"--dangerously-skip-permissions"}
        con_valor = {"--model", "--effort"}
    elif modo == "gemini":
        if prefijo[:1] != ["--skip-trust"]:
            return False
        resto = prefijo[1:]
        sin_valor = {"--yolo"}
        con_valor = {"-m"}
    else:
        return False

    i = 0
    while i < len(resto):
        if resto[i] in sin_valor:
            i += 1
            continue
        if resto[i] not in con_valor or i + 1 >= len(resto):
            return False
        valor = resto[i + 1]
        if resto[i] == "--sandbox" and valor != "read-only":
            return False
        if resto[i] == "--permission-mode" and valor != "plan":
            return False
        if resto[i] == "-c" and not re.fullmatch(
                r'model_reasoning_effort="(low|medium|high|xhigh)"', valor):
            return False
        if resto[i] not in {"--sandbox", "-c"} and not _valor_opcion(valor):
            return False
        i += 2
    return True


def _ref_git(valor):
    return (bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", valor))
            and ".." not in valor and "//" not in valor and "@{" not in valor
            and not valor.endswith((".", "/", ".lock")))


def _usuario_remoto_valido(valor):
    permitidos = frozenset(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
    )
    return bool(valor) and all(c in permitidos for c in valor)


def _host_puerto_remoto_valido(valor):
    host, separador, puerto = valor.partition(":")
    if (not host or any(c.isspace() or c in "/@:" for c in host)
            or (separador and (not puerto or not puerto.isascii()
                               or not puerto.isdigit()))):
        return False
    return True


def _remoto_literal(valor):
    """Reconoce formatos Git cerrados mediante un parser lineal."""
    if (not isinstance(valor, str) or not valor or len(valor) > 4096
            or any(c in valor for c in ("\x00", "\n", "\r"))):
        return False
    if os.path.isabs(valor) or valor.startswith("file:///"):
        return True

    minusculas = valor.lower()
    if minusculas.startswith("https://"):
        autoridad, barra, ruta = valor[8:].partition("/")
        return bool(
            barra and ruta and not any(c.isspace() for c in ruta)
            and _host_puerto_remoto_valido(autoridad)
        )
    if minusculas.startswith("ssh://"):
        autoridad, barra, ruta = valor[6:].partition("/")
        if not barra or not ruta or any(c.isspace() for c in ruta):
            return False
        usuario, arroba, host_puerto = autoridad.partition("@")
        if arroba:
            if not _usuario_remoto_valido(usuario) or "@" in host_puerto:
                return False
        else:
            host_puerto = autoridad
        return _host_puerto_remoto_valido(host_puerto)

    usuario, arroba, destino = valor.partition("@")
    host, dos_puntos, ruta = destino.partition(":")
    return bool(
        arroba and dos_puntos and _usuario_remoto_valido(usuario)
        and host and ruta and not any(c.isspace() or c in "/@:" for c in host)
        and not any(c.isspace() for c in ruta)
    )


def _ref_temporal_auditoria(valor):
    coincidencia = re.fullmatch(
        r"refs/orq-audit/[0-9a-f]{32}/(.{1,4096})", valor, re.DOTALL
    )
    return bool(coincidencia and not any(
        ord(c) < 32 or ord(c) == 127 for c in coincidencia.group(1)
    ))


def _checkout_temporal_verificacion(valor):
    if not isinstance(valor, str) or len(valor) > 4096:
        return False
    raiz = TEMP_ROOT.rstrip(os.sep) or os.sep
    prefijo = raiz if raiz == os.sep else raiz + os.sep
    if not valor.startswith(prefijo):
        return False
    partes = valor[len(prefijo):].split(os.sep)
    if len(partes) != 2 or partes[1] != "checkout":
        return False
    nombre = partes[0]
    return bool(
        nombre.startswith("orq-verify-")
        and len(nombre) > len("orq-verify-")
        and re.fullmatch(r"[A-Za-z0-9_.-]+", nombre)
        and nombre not in (".", "..")
    )


def _git_interno_seguro(opciones):
    if opciones[:len(_GIT_INTERNO)] != _GIT_INTERNO:
        return False
    args = opciones[len(_GIT_INTERNO):]
    if args in (["rev-parse", "--show-toplevel"],
                ["rev-parse", "--show-prefix"], ["rev-parse", "HEAD"],
                ["rev-parse", "HEAD^{tree}"],
                ["rev-parse", "--short", "HEAD"],
                ["rev-parse", "--is-shallow-repository"],
                ["symbolic-ref", "--quiet", "--short", "HEAD"],
                ["remote", "get-url", "origin"],
                ["remote", "get-url", "--push", "origin"],
                ["add", "-A"],
                ["read-tree", "HEAD"], ["read-tree", "--empty"], ["write-tree"],
                ["status", "--porcelain=v2", "-z", "--untracked-files=all"],
                ["diff", "--cached", "--quiet"]):
        return True
    if (len(args) == 7 and args[:5] ==
            ["fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--"]
            and _remoto_literal(args[5])
            and re.fullmatch(
                r"\+refs/heads/\*:refs/orq-audit/[0-9a-f]{32}/\*", args[6]
            )):
        return True
    if (len(args) == 4 and args[:3] == ["ls-remote", "--heads", "--"]
            and _remoto_literal(args[3])):
        return True
    if (len(args) == 3 and args[:2] ==
            ["for-each-ref", "--format=%(refname)%09%(objectname)"]
            and re.fullmatch(r"refs/orq-audit/[0-9a-f]{32}/", args[2])):
        return True
    if (len(args) == 3 and args[:2] ==
            ["for-each-ref", "--format=%(refname)%09%(objectname)"]
            and args[2].startswith("refs/remotes/origin/")):
        return _ref_git(args[2][len("refs/remotes/origin/"):])
    if (len(args) == 2 and args[0] == "read-tree"
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args[1])):
        return True
    if (len(args) == 2 and args[0] == "rev-parse"
            and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\^\{tree\}", args[1])):
        return True
    if args == ["ls-files", "-co", "--exclude-standard", "-z"]:
        return True
    if args == ["ls-files", "-v", "-z"]:
        return True
    if args == ["config", "--local", "--includes", "--name-only",
                "--get-regexp", _CONFIG_GIT_EJECUTABLE]:
        return True
    if args[:3] == ["show-ref", "--verify", "--quiet"] and len(args) == 4:
        for prefijo in ("refs/remotes/origin/", "refs/heads/"):
            if args[3].startswith(prefijo):
                return _ref_git(args[3][len(prefijo):])
        return False
    if len(args) == 4 and args[:3] == ["rev-list", "--left-right", "--count"]:
        comparacion = args[3]
        if re.fullmatch(
                r"(?:[0-9a-f]{40}|[0-9a-f]{64})\.\.\."
                r"(?:[0-9a-f]{40}|[0-9a-f]{64})", comparacion):
            return True
        return False
    if (len(args) == 4 and args[:2] == ["merge-base", "--is-ancestor"]
            and all(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid)
                    for oid in args[2:])):
        return len(args[2]) == len(args[3])
    if len(args) == 2 and args[0] == "rev-list":
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args[1]):
            return True
        if re.fullmatch(
                r"(?:[0-9a-f]{40}|[0-9a-f]{64})\.\.HEAD", args[1]):
            return True
    if (len(args) >= 4 and args[0] == "rev-list"
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args[1])
            and args[2] == "--not"
            and all(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid)
                    for oid in args[3:])):
        return len(args) <= 10_003
    if (len(args) == 3 and args[:2] == ["ls-tree", "-r"]
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args[2])):
        return True
    if (len(args) == 4 and args[:3] == ["ls-tree", "-r", "-z"]
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args[3])):
        return True
    if (len(args) == 6 and args[0] == "commit-tree"
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args[1])
            and args[2] == "-p"
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args[3])
            and args[4] == "-m" and bool(args[5])
            and len(args[5]) <= 200 and "\x00" not in args[5]):
        return True
    if (len(args) == 4 and args[0] == "commit-tree"
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args[1])
            and args[2] == "-m" and bool(args[3])
            and len(args[3]) <= 200 and "\x00" not in args[3]):
        return True
    if (len(args) == 6 and args[:2] == ["worktree", "add"]
            and args[2:4] == ["--no-checkout", "--detach"]
            and _checkout_temporal_verificacion(args[4])
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args[5])):
        return True
    if (len(args) == 4 and args[:3] == ["worktree", "remove", "--force"]
            and _checkout_temporal_verificacion(args[3])):
        return True
    if (len(args) == 4 and args[0] == "update-ref"
            and ((args[1].startswith("refs/heads/")
                  and _ref_git(args[1][len("refs/heads/"):]))
                 or (args[1].startswith("refs/remotes/origin/")
                     and _ref_git(args[1][len("refs/remotes/origin/"):])))
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args[2])
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args[3])):
        return True
    if (len(args) == 4 and args[:2] == ["update-ref", "-d"]
            and _ref_temporal_auditoria(args[2])
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args[3])):
        return True
    if (len(args) == 4 and args[0] == "branch" and args[2] == "--"
            and args[1].startswith("--set-upstream-to=origin/")
            and _ref_git(args[1][len("--set-upstream-to=origin/"):])
            and args[3] == args[1][len("--set-upstream-to=origin/"):]):
        return True
    if (len(args) == 3 and args[:2] == ["reset", "--hard"]
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args[2])):
        return True
    if len(args) == 3 and args[:2] == ["commit", "-m"]:
        return bool(args[2]) and "\x00" not in args[2]
    if (len(args) == 5 and args[0] == "push"
            and args[1].startswith("--force-with-lease=refs/heads/")
            and args[2] == "--" and _remoto_literal(args[3])):
        lease = args[1][len("--force-with-lease="):].split(":", 1)
        refspec = args[4].split(":", 1)
        return (len(refspec) == 2
                and len(lease) == 2
                and bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", refspec[0]))
                and refspec[1].startswith("refs/heads/")
                and _ref_git(refspec[1][len("refs/heads/"):])
                and lease[0] == refspec[1]
                and (lease[1] == "" or bool(re.fullmatch(
                    r"[0-9a-f]{40}|[0-9a-f]{64}", lease[1]
                ))))
    return False


def _git_verificacion_seguro(opciones):
    if opciones[:len(_GIT_VERIFICACION)] != _GIT_VERIFICACION:
        return False
    args = opciones[len(_GIT_VERIFICACION):]
    if args == ["rev-parse", "--is-inside-work-tree"]:
        return True
    if args[:1] == ["status"]:
        return all(x in {"--porcelain", "--short", "--branch", "-sb"}
                   for x in args[1:])
    if args[:1] == ["ls-files"]:
        return all(x in {"--cached", "--modified", "--deleted", "--others",
                         "--exclude-standard", "-c", "-m", "-d", "-o"}
                   for x in args[1:])
    if args[:3] == ["-c", "diff.external=", "diff"]:
        resto = args[3:]
        return (resto in (["--no-ext-diff", "--no-textconv", "--check"],
                          ["--no-ext-diff", "--no-textconv", "--cached", "--check"]))
    if args == ["-c", "log.showSignature=false", "log", "-5",
                "--oneline", "--decorate"]:
        return True
    return False


def _descendientes(pid_raiz):
    hijos = {}
    try:
        entradas = os.listdir("/proc")
    except OSError:
        return []
    for nombre in entradas:
        if not nombre.isdigit():
            continue
        try:
            with open(f"/proc/{nombre}/status", encoding="ascii") as estado:
                ppid = next(
                    int(linea.split()[1]) for linea in estado
                    if linea.startswith("PPid:")
                )
            hijos.setdefault(ppid, []).append(int(nombre))
        except (OSError, StopIteration, ValueError):
            continue
    salida, pila = [], [int(pid_raiz)]
    while pila:
        nuevos = hijos.get(pila.pop(), [])
        salida.extend(nuevos)
        pila.extend(nuevos)
    return salida


def _enviar(sig):
    for pid in reversed(_descendientes(os.getpid())):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _recolectar():
    estados = []
    while True:
        try:
            pid, estado = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        except InterruptedError:
            continue
        if pid == 0:
            break
        estados.append((pid, estado))
    return estados


def _codigo_salida(estado):
    codigo = os.waitstatus_to_exitcode(estado)
    return codigo if codigo >= 0 else 128 - codigo


def _pedir_salida(sig, _marco):
    global _senal_pendiente
    _senal_pendiente = sig


def _terminar_descendientes():
    _enviar(signal.SIGTERM)
    limite = time.monotonic() + 0.4
    while time.monotonic() < limite:
        _recolectar()
        if not _descendientes(os.getpid()):
            return
        time.sleep(0.02)
    # Repetimos la foto: un proceso intermedio pudo morir y transferirnos sus
    # hijos después de la primera enumeración.
    for _ in range(3):
        _enviar(signal.SIGKILL)
        limite = time.monotonic() + 0.25
        while time.monotonic() < limite:
            _recolectar()
            if not _descendientes(os.getpid()):
                return
            time.sleep(0.01)


def _fixture(nombre):
    """Procesos sintéticos fijos usados por las regresiones de lifecycle."""
    if nombre == "infinite":
        while True:
            os.write(1, b"x" * 65536)
    if nombre == "cwd-probe":
        with open("identity", encoding="utf-8") as archivo:
            print(archivo.read().strip(), flush=True)
        return 0
    if nombre == "tree-child":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            time.sleep(1)
    if nombre == "tree-parent":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        hijo = subprocess.Popen([
            "/usr/bin/python3", "-I", "-S", os.path.realpath(__file__),
            "--fixture", "tree-child",
        ])
        with open("pids", "w", encoding="ascii") as archivo:
            archivo.write(f"{os.getpid()} {hijo.pid}")
            archivo.flush()
            os.fsync(archivo.fileno())
        while True:
            time.sleep(1)
    if nombre == "setsid-child":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with open("pids", "a", encoding="ascii") as archivo:
            archivo.write(f" {os.getpid()}")
            archivo.flush()
            os.fsync(archivo.fileno())
        while True:
            with open("heartbeat", "w", encoding="ascii") as archivo:
                archivo.write(str(time.time_ns()))
                archivo.flush()
                os.fsync(archivo.fileno())
            time.sleep(0.03)
    if nombre == "leader-child":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(2)
        with open("heartbeat", "w", encoding="ascii") as archivo:
            archivo.write(str(time.time_ns()))
        while True:
            time.sleep(1)
    if nombre in ("setsid-parent", "leader-exit"):
        if nombre == "setsid-parent":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with open("pids", "w", encoding="ascii") as archivo:
            archivo.write(str(os.getpid()))
            archivo.flush()
            os.fsync(archivo.fileno())
        subprocess.Popen([
            "/usr/bin/python3", "-I", "-S", os.path.realpath(__file__),
            "--fixture", ("leader-child" if nombre == "leader-exit"
                           else "setsid-child"),
        ], start_new_session=True)
        if nombre == "leader-exit":
            print("lider terminado", flush=True)
            return 0
        while True:
            time.sleep(1)
    return 126


def _iniciar_objetivo(modo, opciones):
    """Reconstruye siempre el ejecutable desde una rama literal permitida."""
    if (len(opciones) > 96 or sum(len(x) for x in opciones) > 2 * 1024 * 1024
            or any(not isinstance(x, str) or "\x00" in x for x in opciones)):
        return None
    if modo == "claude":
        if not _opciones_proveedor(modo, opciones):
            return None
        ejecutable = _binario_fijo("claude", proveedor=True)
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "codex":
        if not _opciones_proveedor(modo, opciones):
            return None
        ejecutable = _binario_fijo("codex", proveedor=True)
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "agy":
        if not _opciones_proveedor(modo, opciones):
            return None
        ejecutable = _binario_fijo("agy", proveedor=True)
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "gemini":
        if not _opciones_proveedor(modo, opciones):
            return None
        ejecutable = _binario_fijo("gemini", proveedor=True)
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "git-internal" and _git_interno_seguro(opciones):
        return subprocess.Popen(["/usr/bin/git", *opciones])
    if modo == "git-verification" and _git_verificacion_seguro(opciones):
        return subprocess.Popen(["/usr/bin/git", *opciones])
    if modo == "python-module":
        if (len(opciones) < 2 or opciones[0] != "-m"
                or opciones[1] not in {"unittest", "pytest", "ruff", "mypy"}):
            return None
        return subprocess.Popen(["/usr/bin/python3", "-m", *opciones[1:]])
    if modo == "pytest":
        ejecutable = _binario_fijo("pytest")
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "mypy":
        ejecutable = _binario_fijo("mypy")
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "ruff":
        ejecutable = _binario_fijo("ruff")
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "eslint":
        ejecutable = _binario_fijo("eslint")
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "node":
        ejecutable = _binario_fijo("node")
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "npm":
        ejecutable = _binario_fijo("npm")
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "pnpm":
        ejecutable = _binario_fijo("pnpm")
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "yarn":
        ejecutable = _binario_fijo("yarn")
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "cargo":
        ejecutable = _binario_fijo("cargo")
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "go":
        ejecutable = _binario_fijo("go")
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "make":
        ejecutable = _binario_fijo("make")
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "tsc":
        ejecutable = _binario_fijo("tsc")
        return subprocess.Popen([ejecutable, *opciones]) if ejecutable else None
    if modo == "systemd-analyze":
        return subprocess.Popen(["/usr/bin/systemd-analyze", *opciones])
    if modo == "pwd" and not opciones:
        return subprocess.Popen(["/usr/bin/pwd"])
    if modo == "ls" and opciones == ["-la"]:
        return subprocess.Popen(["/usr/bin/ls", "-la"])
    if modo == "bash-n" and opciones[:1] == ["-n"]:
        return subprocess.Popen(["/usr/bin/bash", "-n", *opciones[1:]])
    if modo == "sh-n" and opciones[:1] == ["-n"]:
        return subprocess.Popen(["/usr/bin/sh", "-n", *opciones[1:]])
    if modo == "scanner":
        scanner = os.path.realpath(os.path.join(
            os.path.dirname(os.path.realpath(__file__)), "tools", "scan-secretos.sh"
        ))
        if not opciones or os.path.realpath(opciones[0]) != scanner:
            return None
        return subprocess.Popen(["/usr/bin/bash", scanner, *opciones[1:]])
    if modo == "fixture":
        propio = os.path.realpath(__file__)
        if (len(opciones) != 3 or os.path.realpath(opciones[0]) != propio
                or opciones[1] != "--fixture"
                or opciones[2] not in {"infinite", "cwd-probe", "tree-parent",
                                       "setsid-parent", "leader-exit"}):
            return None
        return subprocess.Popen([
            "/usr/bin/python3", "-I", "-S", propio,
            "--fixture", opciones[2],
        ])
    return None


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--fixture":
        return _fixture(args[1])
    if len(args) < 2 or args[0] != "--mode" or not _activar_subreaper():
        print("orqrun: contencion Linux no disponible", file=sys.stderr)
        return 126
    modo, opciones = args[1], args[2:]

    signal.signal(signal.SIGTERM, _pedir_salida)
    signal.signal(signal.SIGINT, _pedir_salida)
    try:
        objetivo = _iniciar_objetivo(modo, opciones)
    except OSError as exc:
        print(f"orqrun: no pude iniciar el objetivo: {exc}", file=sys.stderr)
        return 127
    if objetivo is None:
        print("orqrun: modo u opciones fuera de la capacidad", file=sys.stderr)
        return 126

    estado_objetivo = None
    while estado_objetivo is None and not _senal_pendiente:
        for pid, estado in _recolectar():
            if pid == objetivo.pid:
                estado_objetivo = estado
        if estado_objetivo is None:
            time.sleep(0.01)

    _terminar_descendientes()
    _recolectar()
    if _senal_pendiente:
        return 128 + _senal_pendiente
    return _codigo_salida(estado_objetivo)


if __name__ == "__main__":
    raise SystemExit(main())
