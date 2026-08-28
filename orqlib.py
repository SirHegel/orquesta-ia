"""Nucleo compartido del orquestador multi-cuenta.

Estado en disco con bloqueo fcntl: cualquier numero de terminales puede usar
el sistema a la vez sin corromper el ledger ni los contadores.
"""
import json, os, re, shlex, shutil, subprocess, sys, threading, time, datetime, fcntl, contextlib, uuid, signal, hashlib, glob, tempfile, stat, pwd, secrets
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

_SUBPROCESS_RUN_ORIGINAL = subprocess.run
_SYSTEMD_USUARIO_CACHE = {}

# El módulo vive dentro de la instalación y es una raíz más fiable que una
# variable heredada. ``shell.sh`` aún usa ORQ_HOME para localizar el ejecutable,
# pero el proceso Python deriva su estado desde el archivo que realmente cargó.
CODIGO_ORQUESTA = os.path.dirname(os.path.realpath(__file__))
BASE = CODIGO_ORQUESTA
HOME_USUARIO = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
TEMP_ROOT = os.path.realpath("/tmp")
ACCOUNTS = os.path.join(BASE, "accounts")
ACCOUNTS_USUARIO = os.path.join(HOME_USUARIO, ".local", "share", "orquesta", "accounts")
PROFILES = os.path.join(BASE, "profiles.json")
LEDGER = os.path.join(BASE, "state", "ledger.jsonl")
LIMITS = os.path.join(BASE, "state", "limits.json")
SCORES = os.path.join(BASE, "state", "scores.json")
LOCK = os.path.join(BASE, "state", ".lock")
CLAVE_LIMITE_ANTIGRAVITY = "@antigravity-global"
MAX_REFS_REMOTAS = 10_000


class ErrorConfiguracion(RuntimeError):
    """La configuracion persistida existe, pero no es segura ni utilizable."""

# MiniMax habla el protocolo de Anthropic: reusamos el binario "claude"
# apuntandolo a su endpoint. Por eso nunca exportamos estas variables de
# forma global: pisarian la cuenta Claude real de las terminales.
MINIMAX_BASE_URL = "https://api.minimax.io/anthropic"
MINIMAX_BASE_URL_CN = "https://api.minimaxi.com/anthropic"
MINIMAX_MODELO = "MiniMax-M3[1m]"
MINIMAX_ENDPOINTS = {MINIMAX_BASE_URL, MINIMAX_BASE_URL_CN}

_GIT_INTERNO = [
    "--no-pager", "--no-replace-objects",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
    "-c", "core.commitGraph=false", "-c", "fetch.writeCommitGraph=false",
    "-c", "core.attributesFile=/dev/null", "-c", "credential.helper=",
    "-c", "core.askPass=", "-c", "core.sshCommand=/usr/bin/ssh",
    "-c", "ssh.variant=ssh", "-c", "core.gitProxy=",
    "-c", "protocol.ext.allow=never", "-c", "diff.external=",
    "-c", "advice.graftFileDeprecated=false",
    "-c", "commit.gpgSign=false",
    "-c", "remote.origin.uploadpack=git-upload-pack",
    "-c", "remote.origin.receivepack=git-receive-pack",
]
_GIT_VERIFICACION = [
    "--no-pager", "--no-replace-objects",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
    "-c", "core.commitGraph=false",
    "-c", "advice.graftFileDeprecated=false",
]

TAREAS = ["code", "agentic", "reasoning", "review", "writing",
          "research", "edicion", "imagen", "bulk"]

# Capacidades por motor. El chat de maxima potencia es Claude/Codex;
# Antigravity se reserva para generar recursos visuales con Nano Banana.
PROVEEDORES_TEXTO = {"claude", "gpt", "minimax"}
PROVEEDORES_IMAGEN = {"antigravity"}

# Potencia del motor efectivo, no del nombre de la cuenta. Claude Opus y el
# Codex configurado compiten en el nivel maximo; AGY tiene ese nivel solo para
# su capacidad visual. Un perfil puede ajustar ``power`` (numero o por tarea).
POTENCIA_BASE = {"claude": 10.0, "gpt": 10.0, "antigravity": 10.0, "minimax": 8.5}
ID_PERFIL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}-[0-9]{10,30}$")
SESION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
MIN_MUESTRA_REPARTO = 10_000


def id_perfil_valido(pid):
    return bool(ID_PERFIL_RE.fullmatch(str(pid or "")))


def base_url_minimax(p):
    """Acepta solo endpoints HTTPS oficiales para no exfiltrar la API key."""
    valor = p.get("base_url") or MINIMAX_BASE_URL
    if not isinstance(valor, str) or valor not in MINIMAX_ENDPOINTS:
        raise ValueError("endpoint MiniMax no permitido")
    return valor


def run_id_valido(run_id):
    """Los ids persistidos siempre son ``perfil-time_ns`` y nunca una ruta."""
    return bool(RUN_ID_RE.fullmatch(str(run_id or "")))


def id_sesion_seguro(valor, fallback):
    """Convierte el identificador local de terminal en un único nombre de archivo.

    ``ORQ_SESION`` es una comodidad local, no una ruta. Si un lanzador externo
    entrega caracteres de ruta, conservamos estabilidad con un hash en vez de
    interpolarlos en ``state/sesiones``.
    """
    crudo = str(valor or "")
    if crudo:
        # El nombre visible de la terminal no necesita formar parte de una ruta.
        # El hash conserva la relación estable entre reinicios sin propagar datos
        # heredados hacia el sistema de archivos.
        return "sesion-" + hashlib.sha256(crudo.encode("utf-8")).hexdigest()[:20]
    alterno = str(fallback or "")
    if SESION_RE.fullmatch(alterno):
        return alterno
    return "sesion-" + hashlib.sha256(
        (alterno or "sesion").encode("utf-8")
    ).hexdigest()[:20]


def _expandir_usuario(valor):
    """Expande solo ``~`` del UID real; nunca confía en HOME heredado."""
    texto = str(valor)
    if texto == "~":
        return HOME_USUARIO
    if texto.startswith("~/"):
        return os.path.join(HOME_USUARIO, texto[2:])
    return texto


def ruta_contenida(raiz, ruta):
    """Devuelve la ruta canónica solo cuando permanece dentro de ``raiz``."""
    base = os.path.realpath(os.path.abspath(_expandir_usuario(raiz)))
    destino = os.path.realpath(os.path.abspath(_expandir_usuario(ruta)))
    prefijo = base if base.endswith(os.sep) else base + os.sep
    comprobable = destino if destino.endswith(os.sep) else destino + os.sep
    if not comprobable.startswith(prefijo):
        return None
    return comprobable.rstrip(os.sep) or os.sep


def _ruta_en_alguna_raiz(ruta, raices, permitir_raiz=True):
    """Normaliza ``ruta`` y exige una de las capacidades de directorio dadas."""
    for raiz in raices:
        segura = ruta_contenida(raiz, ruta)
        if segura is not None and (permitir_raiz or segura != os.path.realpath(raiz)):
            return segura
    return None


def _ruta_sin_enlaces(raiz, ruta):
    """Exige contención canónica y rechaza enlaces en cualquier componente."""
    base_lexica = os.path.abspath(_expandir_usuario(raiz))
    ruta_lexica = os.path.abspath(_expandir_usuario(ruta))
    prefijo = base_lexica.rstrip(os.sep) + os.sep
    comprobable = ruta_lexica.rstrip(os.sep) + os.sep
    # El guard es incondicional, incluida la raíz exacta. Así ninguna rama
    # llega al walker sin demostrar antes la capacidad léxica.
    if not comprobable.startswith(prefijo):
        return None
    # Todo acceso posterior deriva de la misma expresión normalizada que pasó
    # el guard de prefijo; no se reutiliza la entrada previa a la comprobación.
    ruta_lexica = comprobable.rstrip(os.sep) or os.sep
    segura = ruta_contenida(base_lexica, ruta_lexica)
    if segura is None:
        return None
    # Recorremos la expresión léxica, no ``realpath``: de otro modo un enlace
    # que apunta de vuelta a la misma raíz desaparecería antes de comprobarlo.
    cursor = os.path.sep if os.path.isabs(ruta_lexica) else ""
    for parte in ruta_lexica.split(os.sep):
        if not parte:
            continue
        cursor = os.path.join(cursor, parte)
        try:
            if stat.S_ISLNK(os.lstat(cursor).st_mode):
                return None
        except FileNotFoundError:
            # El componente final puede crearse después mediante una operación
            # descriptor-relativa; los padres ausentes fallarán cerrados allí.
            continue
        except OSError:
            return None
    return segura


def ruta_cuenta(pid, p, campo=None, predeterminado=None):
    """Resuelve un archivo privado sin permitir que salga del home de la cuenta."""
    home = home_de(pid, p)
    valor = p.get(campo) if campo else predeterminado
    if valor in (None, ""):
        valor = predeterminado
    if valor in (None, ""):
        return None
    crudo = _expandir_usuario(valor)
    candidato = crudo if os.path.isabs(crudo) else os.path.join(home, crudo)
    return _ruta_sin_enlaces(home, candidato)


def ruta_trabajo_segura(ruta, raices=None):
    """Autoriza un cwd canónico dentro de áreas de trabajo controladas.

    Por defecto se permite el directorio personal, la instalación y un
    directorio temporal privado del usuario. ``raices`` existe para llamadas
    internas que conceden explícitamente una capacidad más estrecha.
    """
    if ruta in (None, ""):
        return None
    candidato = os.path.abspath(_expandir_usuario(ruta))
    autorizadas = tuple(raices) if raices is not None else (
        HOME_USUARIO, BASE,
    )
    segura = None
    for raiz in autorizadas:
        segura = _ruta_sin_enlaces(raiz, candidato)
        if segura is not None:
            break
    if segura is None and raices is None:
        temporal = TEMP_ROOT
        segura = _ruta_sin_enlaces(temporal, candidato)
        if segura == os.path.realpath(temporal):
            segura = None
        if segura is not None:
            try:
                relativa = os.path.relpath(segura, os.path.realpath(temporal))
                privada = os.path.join(
                    os.path.realpath(temporal), relativa.split(os.sep, 1)[0]
                )
                estado = os.stat(privada, follow_symlinks=False)
                if (estado.st_uid != os.getuid() or estado.st_mode & 0o077
                        or os.path.islink(privada)):
                    segura = None
            except OSError:
                segura = None
    return segura if segura and os.path.isdir(segura) else None


def preparar_ruta_trabajo(ruta, crear=False):
    """Valida un cwd y, si se autoriza, crea descendientes con ``mkdirat``."""
    segura = ruta_trabajo_segura(ruta)
    if segura or not crear or ruta in (None, ""):
        return segura
    candidato = os.path.abspath(_expandir_usuario(ruta))
    segura = None
    for raiz in (HOME_USUARIO, BASE):
        opcion = _ruta_sin_enlaces(raiz, candidato)
        if opcion is not None:
            segura = opcion
            break
    if segura is None:
        temporal = TEMP_ROOT
        dentro_tmp = _ruta_sin_enlaces(temporal, candidato)
        if dentro_tmp and dentro_tmp != temporal:
            relativa = os.path.relpath(dentro_tmp, temporal)
            privada = os.path.join(temporal, relativa.split(os.sep, 1)[0])
            try:
                estado = os.stat(privada, follow_symlinks=False)
                if (stat.S_ISDIR(estado.st_mode) and estado.st_uid == os.getuid()
                        and not estado.st_mode & 0o077 and not os.path.islink(privada)):
                    segura = _ruta_sin_enlaces(privada, candidato)
            except OSError:
                segura = None
    if segura is None:
        return None
    try:
        with _abrir_directorio_seguro(segura, crear=True):
            pass
    except OSError:
        return None
    return ruta_trabajo_segura(segura)


def admite_tarea(p, tarea):
    """Indica si un perfil puede recibir la tarea, antes de puntuarlo.

    ``allowed_tasks`` permite restringir un perfil. No puede ampliar la
    capacidad real del motor: uno visual nunca termina contestando el chat
    normal solo porque los motores de texto tengan menos cuota.
    """
    prov = p.get("provider")
    capacidad_motor = (prov in PROVEEDORES_IMAGEN if tarea == "imagen"
                       else prov in PROVEEDORES_TEXTO)
    if not capacidad_motor:
        return False
    explicitas = p.get("allowed_tasks")
    if explicitas is None:
        return True
    if isinstance(explicitas, str):
        explicitas = [explicitas]
    # La configuracion puede restringir capacidades, nunca inventarlas.
    return tarea in explicitas


def potencia_perfil(p, tarea):
    valor = p.get("power")
    if isinstance(valor, dict):
        valor = valor.get(tarea)
    if valor is None:
        valor = POTENCIA_BASE.get(p.get("provider"), 0)
    try:
        return max(0.0, float(valor))
    except (TypeError, ValueError):
        return 0.0

# Ventana de recarga tipica por plan (horas). Ajustable por perfil.
VENTANA_PLAN = {"max": 5, "pro": 5, "team": 5, "api": 1, "free": 5, "desconocido": 5}


@contextlib.contextmanager
def bloqueo():
    f = _abrir_lock(LOCK)
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


@contextlib.contextmanager
def _abrir_directorio_seguro(path, crear=False, modo=0o700):
    """Abre un directorio absoluto componente a componente, sin symlinks.

    El descriptor resultante es una capacidad estable aunque otro proceso
    renombre rutas mientras dura la operación. Si ``crear`` está activo, cada
    componente ausente se crea con ``mkdirat`` y se vuelve a abrir sin seguir
    enlaces; una carrera que coloque otra entrada hace fallar la operación.
    """
    absoluto = os.path.abspath(_expandir_usuario(path))
    if not os.path.isabs(absoluto):
        raise OSError("el directorio seguro debe ser absoluto")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(os.path.sep, flags)
    try:
        for parte in absoluto.split(os.sep):
            if not parte:
                continue
            try:
                siguiente = os.open(parte, flags, dir_fd=fd)
            except FileNotFoundError:
                if not crear:
                    raise
                try:
                    os.mkdir(parte, modo, dir_fd=fd)
                except FileExistsError:
                    # Otro proceso seguro pudo crear el mismo componente entre
                    # openat y mkdirat. La reapertura O_NOFOLLOW revalida tipo.
                    pass
                siguiente = os.open(parte, flags, dir_fd=fd)
            if not stat.S_ISDIR(os.fstat(siguiente).st_mode):
                os.close(siguiente)
                raise OSError("un componente no es un directorio regular")
            os.close(fd)
            fd = siguiente
        yield fd
    finally:
        os.close(fd)


def _adquirir_capacidad_trabajo(ruta, raices=None):
    """Mantiene un cwd autorizado como descriptor, no como nombre mutable.

    La validacion de política decide qué ruta puede adquirirse y el walker
    ``openat``/``O_NOFOLLOW`` abre exactamente ese directorio. Desde ese punto
    los consumidores heredan el descriptor: renombrar la entrada o sustituirla
    por un symlink no cambia la capacidad ya adquirida.
    """
    if ruta in (None, ""):
        raise OSError("directorio de trabajo no autorizado")
    candidato = os.path.abspath(_expandir_usuario(ruta))
    bases = tuple(raices) if raices is not None else (HOME_USUARIO, BASE)
    opciones = []
    for base in bases:
        lexica = os.path.abspath(_expandir_usuario(base))
        prefijo = lexica.rstrip(os.sep) + os.sep
        comprobable = candidato.rstrip(os.sep) + os.sep
        if comprobable.startswith(prefijo):
            opciones.append((lexica, False))
    if raices is None:
        temporal = os.path.abspath(TEMP_ROOT)
        prefijo_tmp = temporal.rstrip(os.sep) + os.sep
        comprobable = candidato.rstrip(os.sep) + os.sep
        if comprobable.startswith(prefijo_tmp) and candidato != temporal:
            opciones.append((temporal, True))
    if not opciones:
        raise OSError("directorio de trabajo fuera de las raices autorizadas")

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    ultimo_error = None
    # La raíz más específica gana si la instalación vive dentro del HOME.
    for base, es_temporal in sorted(opciones, key=lambda x: len(x[0]), reverse=True):
        try:
            with _abrir_directorio_seguro(base) as base_fd:
                base_visible = os.path.realpath(f"/proc/self/fd/{base_fd}")
                relativa = os.path.relpath(candidato, base)
                partes = [] if relativa == "." else relativa.split(os.sep)
                if any(p in ("", ".", "..") for p in partes):
                    raise OSError("componente cwd invalido")
                fd = os.dup(base_fd)
                try:
                    for indice, parte in enumerate(partes):
                        siguiente = os.open(parte, flags, dir_fd=fd)
                        os.close(fd)
                        fd = siguiente
                        estado = os.fstat(fd)
                        if not stat.S_ISDIR(estado.st_mode):
                            raise OSError("un componente cwd no es directorio")
                        if es_temporal and indice == 0 and (
                                estado.st_uid != os.getuid()
                                or estado.st_mode & 0o077):
                            raise OSError("raiz temporal no privada")
                    estado = os.fstat(fd)
                    if not stat.S_ISDIR(estado.st_mode):
                        raise OSError("la capacidad cwd no es directorio")
                    adquirida = os.path.realpath(f"/proc/self/fd/{fd}")
                    prefijo = base_visible.rstrip(os.sep) + os.sep
                    comprobable = adquirida.rstrip(os.sep) + os.sep
                    if (adquirida.endswith(" (deleted)")
                            or not comprobable.startswith(prefijo)):
                        raise OSError("el cwd cambio durante la adquisicion")
                except Exception:
                    os.close(fd)
                    raise
                return adquirida, fd
        except OSError as exc:
            ultimo_error = exc
    raise OSError("no pude adquirir el cwd de forma estable") from ultimo_error


@contextlib.contextmanager
def _capacidad_trabajo(ruta, raices=None):
    """Contexto que cierra la capacidad sin capturar fallos del consumidor."""
    adquirida, fd = _adquirir_capacidad_trabajo(ruta, raices=raices)
    try:
        yield adquirida, fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def _capacidad_raiz_git(cwd_fd):
    """Deriva el top-level Git ascendiendo desde un cwd ya adquirido.

    Git solo aporta cuántos componentes separan el cwd de la raíz. Cada
    ascenso usa ``openat('..')`` sobre el descriptor anterior, de modo que no
    se reabre el nombre mutable devuelto por ``--show-toplevel``.
    """
    prefijo_r = _git(".", "rev-parse", "--show-prefix", cwd_fd=cwd_fd)
    if not prefijo_r or prefijo_r.returncode != 0:
        raise OSError("el cwd adquirido no pertenece a un repositorio Git")
    crudo = prefijo_r.stdout
    if (not isinstance(crudo, str) or not crudo.endswith("\n")
            or "\n" in crudo[:-1] or "\r" in crudo or "\x00" in crudo):
        raise OSError("prefijo Git invalido")
    prefijo = crudo[:-1]
    if prefijo and not prefijo.endswith("/"):
        raise OSError("prefijo Git incompleto")
    partes = prefijo[:-1].split("/") if prefijo else []
    if any(parte in ("", ".", "..") for parte in partes):
        raise OSError("prefijo Git inseguro")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) \
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    raiz_fd = os.dup(cwd_fd)
    try:
        for _parte in partes:
            padre_fd = os.open("..", flags, dir_fd=raiz_fd)
            os.close(raiz_fd)
            raiz_fd = padre_fd
        estado = os.fstat(raiz_fd)
        if not stat.S_ISDIR(estado.st_mode):
            raise OSError("raiz Git no es un directorio")
        # Confirma sobre el descriptor derivado que ya estamos en el top-level.
        raiz_r = _git(".", "rev-parse", "--show-prefix", cwd_fd=raiz_fd)
        if not raiz_r or raiz_r.returncode != 0 or raiz_r.stdout != "\n":
            raise OSError("raiz Git incoherente")
        visible = os.path.realpath(f"/proc/self/fd/{raiz_fd}")
        if visible.endswith(" (deleted)"):
            raise OSError("raiz Git eliminada")
        yield visible, raiz_fd, prefijo
    finally:
        os.close(raiz_fd)


def cambiar_directorio_trabajo(ruta, raices=None):
    """Cambia cwd mediante una capacidad estable y devuelve la ruta validada."""
    with _capacidad_trabajo(ruta, raices=raices) as (segura, fd):
        os.fchdir(fd)
        return segura


@contextlib.contextmanager
def _abrir_regular(path, errors=None, mode="r", privado=False):
    """Abre un archivo regular mediante su padre estable y sin symlinks."""
    absoluto = os.path.abspath(_expandir_usuario(path))
    directorio, nombre = os.path.split(absoluto)
    if not nombre or nombre in (".", "..") or os.sep in nombre:
        raise OSError("nombre de archivo invalido")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with _abrir_directorio_seguro(directorio) as dir_fd:
        fd = os.open(nombre, flags, dir_fd=dir_fd)
        try:
            estado = os.fstat(fd)
            if not stat.S_ISREG(estado.st_mode):
                raise OSError("la ruta no es un archivo regular")
            if privado and (estado.st_uid != os.getuid() or estado.st_nlink != 1
                            or estado.st_mode & 0o077):
                raise OSError(
                    "el archivo privado tiene propietario, modo o enlaces inseguros"
                )
            opciones = {} if "b" in mode else {"errors": errors}
            with os.fdopen(fd, mode, **opciones) as f:
                fd = None
                yield f
        finally:
            if fd is not None:
                os.close(fd)


def _identidad_estado(estado):
    return [estado.st_mtime_ns, estado.st_dev, estado.st_ino, estado.st_size]


@contextlib.contextmanager
def _abrir_regular_identidad(path, identidad=None, errors=None, mode="r"):
    """Reabre un archivo privado y exige la identidad observada por el walker."""
    with _abrir_regular(path, errors=errors, mode=mode, privado=True) as archivo:
        if identidad is not None and _identidad_estado(
                os.fstat(archivo.fileno())) != list(identidad):
            raise OSError("el archivo cambio despues de enumerarlo")
        yield archivo


def _abrir_lock(path):
    """Abre un lock privado sin seguir enlaces ni aceptar hardlinks."""
    absoluto = os.path.abspath(_expandir_usuario(path))
    directorio, nombre = os.path.split(absoluto)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with _abrir_directorio_seguro(directorio, crear=True) as dir_fd:
        fd = os.open(nombre, flags, 0o600, dir_fd=dir_fd)
        try:
            estado = os.fstat(fd)
            if (not stat.S_ISREG(estado.st_mode) or estado.st_uid != os.getuid()
                    or estado.st_nlink != 1):
                raise OSError("lock inseguro")
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, "a+")
        except Exception:
            os.close(fd)
            raise


def _anexar_texto(path, contenido):
    """Anexa a un regular privado usando un descriptor de directorio estable."""
    absoluto = os.path.abspath(_expandir_usuario(path))
    directorio, nombre = os.path.split(absoluto)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with _abrir_directorio_seguro(directorio, crear=True) as dir_fd:
        fd = os.open(nombre, flags, 0o600, dir_fd=dir_fd)
        try:
            estado = os.fstat(fd)
            if (not stat.S_ISREG(estado.st_mode) or estado.st_uid != os.getuid()
                    or estado.st_nlink != 1):
                raise OSError("archivo de estado inseguro")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a") as archivo:
                fd = None
                archivo.write(str(contenido))
                archivo.flush()
                os.fsync(archivo.fileno())
            os.fsync(dir_fd)
        finally:
            if fd is not None:
                os.close(fd)


def _vaciar_regular_privado_at(dir_fd, nombre, esperado=None):
    """Vacía un regular estable sin borrar una entrada que pueda ser sustituida.

    ``unlinkat`` no permite ligar el borrado al descriptor del archivo ya
    validado. Una sustitución entre ``stat`` y ``unlink`` podría borrar otro
    inode. Conservamos por ello la entrada vacía y hacemos ``ftruncate`` sobre
    el descriptor abierto con ``O_NOFOLLOW``, tras comprobar su identidad.
    """
    if not isinstance(nombre, str) or not nombre or nombre in (".", ".."):
        raise OSError("nombre de archivo invalido")
    if esperado is None:
        esperado = os.stat(nombre, dir_fd=dir_fd, follow_symlinks=False)
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) \
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(nombre, flags, dir_fd=dir_fd)
    try:
        estado = os.fstat(fd)
        identidad = lambda st: (st.st_dev, st.st_ino, st.st_mode,
                                 st.st_uid, st.st_nlink)
        if (identidad(estado) != identidad(esperado)
                or not stat.S_ISREG(estado.st_mode)
                or estado.st_uid != os.getuid() or estado.st_nlink != 1):
            raise OSError("archivo privado inseguro o sustituido")
        os.ftruncate(fd, 0)
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def _eliminar_regular_privado(path):
    """Inutiliza el contenido privado y conserva una entrada vacía estable."""
    absoluto = os.path.abspath(_expandir_usuario(path))
    directorio, nombre = os.path.split(absoluto)
    if not nombre or nombre in (".", ".."):
        raise OSError("nombre de archivo invalido")
    with _abrir_directorio_seguro(directorio) as dir_fd:
        esperado = os.stat(nombre, dir_fd=dir_fd, follow_symlinks=False)
        _vaciar_regular_privado_at(dir_fd, nombre, esperado)
        os.fsync(dir_fd)


def _leer_texto(path, default=None, limite=8 * 1024 * 1024, privado=True):
    """Lee solo un archivo regular y rechaza el seguimiento del enlace final."""
    try:
        with _abrir_regular(path, privado=privado) as f:
            contenido = f.read(limite + 1)
            return contenido if len(contenido) <= limite else default
    except (OSError, UnicodeError):
        return default


def _lineas_acotadas(archivo, bytes_totales=32 * 1024 * 1024,
                      bytes_linea=1024 * 1024, filas=100_000):
    """Itera líneas completas sin materializar una línea o archivo hostil."""
    restantes = max(0, int(bytes_totales))
    for _ in range(max(0, int(filas))):
        if restantes <= 0:
            return
        linea = archivo.readline(min(bytes_linea + 1, restantes + 1))
        if not linea:
            return
        restantes -= len(linea)
        if len(linea) > bytes_linea or not linea.endswith("\n"):
            while linea and not linea.endswith("\n") and restantes > 0:
                linea = archivo.readline(min(64 * 1024, restantes))
                restantes -= len(linea)
            continue
        yield linea


def _leer(path, default):
    contenido = _leer_texto(path)
    if contenido is None:
        return default
    try:
        return json.loads(contenido)
    except (ValueError, TypeError):
        return default


def _reemplazar_atomico(path, escritor):
    """Publica bytes con ``openat``/``renameat`` atómicos y privados."""
    absoluto = os.path.abspath(_expandir_usuario(path))
    directorio, nombre = os.path.split(absoluto)
    if not nombre or nombre in (".", "..") or os.sep in nombre:
        raise OSError("nombre de archivo invalido")
    tmp = None
    with _abrir_directorio_seguro(directorio, crear=True) as dir_fd:
        try:
            estado = os.stat(nombre, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            estado = None
        if estado is not None and not stat.S_ISREG(estado.st_mode):
            raise OSError("destino de estado no es un archivo regular")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        for _ in range(32):
            candidato = f".{nombre}.{uuid.uuid4().hex}.tmp"
            try:
                fd = os.open(candidato, flags, 0o600, dir_fd=dir_fd)
                tmp = candidato
                break
            except FileExistsError:
                continue
        if tmp is None:
            raise OSError("no pude reservar un archivo temporal privado")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                escritor(f)
                f.flush()
                os.fsync(f.fileno())
            # Revalidar el tipo justo antes del reemplazo mantiene el fallo
            # cerrado. ``renameat`` nunca sigue la entrada de destino.
            try:
                estado = os.stat(nombre, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                estado = None
            if estado is not None and not stat.S_ISREG(estado.st_mode):
                raise OSError("destino de estado cambio a una entrada insegura")
            os.replace(tmp, nombre, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            tmp = None
            os.fsync(dir_fd)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass


def _escribir_texto(path, contenido):
    datos = str(contenido).encode("utf-8")
    _reemplazar_atomico(path, lambda archivo: archivo.write(datos))


def _copiar_regular_atomico(origen, destino, identidad=None):
    """Publica una copia privada atómica y falla si ``destino`` ya existe."""
    absoluto = os.path.abspath(_expandir_usuario(destino))
    directorio, nombre = os.path.split(absoluto)
    if not nombre or nombre in (".", ".."):
        raise OSError("nombre de destino invalido")
    temporal = None
    with _abrir_regular_identidad(origen, identidad, mode="rb") as fuente, \
            _abrir_directorio_seguro(directorio, crear=True) as dir_fd:
        if os.fstat(fuente.fileno()).st_size > 128 * 1024 * 1024:
            raise OSError("imagen demasiado grande")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        for _ in range(32):
            temporal = f".imagen-{uuid.uuid4().hex}.tmp"
            try:
                fd = os.open(temporal, flags, 0o600, dir_fd=dir_fd)
                break
            except FileExistsError:
                temporal = None
        if temporal is None:
            raise OSError("no pude reservar una copia temporal")
        try:
            with os.fdopen(fd, "wb") as salida:
                shutil.copyfileobj(fuente, salida, 1024 * 1024)
                salida.flush()
                os.fsync(salida.fileno())
            # linkat publica solo si el nombre sigue libre; nunca reemplaza una
            # imagen creada por otro proceso entre la selección y la copia.
            os.link(temporal, nombre, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                    follow_symlinks=False)
            os.unlink(temporal, dir_fd=dir_fd)
            temporal = None
            os.fsync(dir_fd)
        finally:
            if temporal is not None:
                try:
                    os.unlink(temporal, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
    return absoluto


def _copiar_regular_unico(origen, destino, identidad=None, intentos=100):
    """Elige un sufijo mediante publicación O_EXCL, sin prueba TOCTOU previa."""
    tronco, extension = os.path.splitext(destino)
    for numero in range(intentos):
        candidato = destino if numero == 0 else f"{tronco}-{numero}{extension}"
        try:
            return _copiar_regular_atomico(origen, candidato, identidad)
        except FileExistsError:
            continue
    raise OSError("demasiadas colisiones al publicar la imagen")


def _escribir(path, data):
    _escribir_texto(path, json.dumps(data, indent=2, ensure_ascii=False))


def cfg():
    """Carga ``profiles.json`` sin convertir errores en una config vacia.

    La ausencia del archivo representa una instalacion aun no configurada. En
    cambio, si la entrada existe pero no puede abrirse como regular privado, es
    demasiado grande o contiene JSON invalido, continuar con un diccionario
    vacio permitiria que una escritura posterior reemplazara configuracion
    real que nunca se llego a leer. Esos casos fallan de forma visible.
    """
    try:
        with _abrir_regular(PROFILES, privado=True) as archivo:
            contenido = archivo.read(8 * 1024 * 1024 + 1)
    except FileNotFoundError:
        return {"profiles": {}}
    except (OSError, UnicodeError) as exc:
        raise ErrorConfiguracion(
            "profiles.json no es un archivo privado legible; "
            "usa un regular propio con modo 0600"
        ) from exc
    if len(contenido) > 8 * 1024 * 1024:
        raise ErrorConfiguracion("profiles.json excede el limite de 8 MiB")
    try:
        config = json.loads(contenido)
    except (ValueError, TypeError) as exc:
        raise ErrorConfiguracion("profiles.json no contiene JSON valido") from exc
    if (not isinstance(config, dict)
            or not isinstance(config.get("profiles", {}), dict)):
        raise ErrorConfiguracion(
            "profiles.json debe ser un objeto con un mapa 'profiles'"
        )
    return config


def guardar_cfg(c):
    if (not isinstance(c, dict)
            or not isinstance(c.get("profiles", {}), dict)):
        raise ErrorConfiguracion("no se escribio una configuracion invalida")
    _escribir(PROFILES, c)


def limites():
    return _leer(LIMITS, {})


def scores():
    return _leer(SCORES, {})


def ahora():
    return datetime.datetime.now()


def hoy():
    return datetime.date.today().isoformat()


def ledger_rows(dias=None):
    rows = []
    try:
        with _abrir_regular(LEDGER, privado=True) as f:
            numero = 0
            restantes = 32 * 1024 * 1024
            while numero < 100_000 and restantes > 0:
                line = f.readline(min(1_000_001, restantes + 1))
                if not line:
                    break
                restantes -= len(line)
                numero += 1
                if len(line) > 1_000_000 or not line.endswith("\n"):
                    # Drena el resto de una línea sobredimensionada en bloques.
                    while line and not line.endswith("\n") and restantes > 0:
                        line = f.readline(min(64 * 1024, restantes))
                        restantes -= len(line)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except (OSError, UnicodeError):
        pass
    if dias:
        corte = (ahora() - datetime.timedelta(days=dias)).date().isoformat()
        rows = [r for r in rows if r.get("fecha", "") >= corte]
    return rows


def log(entry):
    with bloqueo():
        _anexar_texto(LEDGER, json.dumps(entry, ensure_ascii=False) + "\n")


def gastado_hoy(pid):
    return sum(r.get("tokens", 0) for r in ledger_rows()
               if r.get("perfil") == pid and r.get("fecha") == hoy())


def gastado_ventana(pid, horas):
    """Tokens gastados dentro de la ventana de recarga vigente."""
    corte = ahora() - datetime.timedelta(hours=horas)
    tot = 0
    for r in ledger_rows(dias=3):
        if r.get("perfil") != pid:
            continue
        try:
            ts = datetime.datetime.fromisoformat(r["ts"])
        except Exception:
            continue
        if ts >= corte:
            tot += r.get("tokens", 0)
    return tot


# ---------------- cuentas ----------------
def home_de(pid, p):
    """Devuelve solo un home administrado o el home oficial del proveedor."""
    if not id_perfil_valido(pid):
        raise ValueError("id de cuenta invalido")
    h = p.get("home")
    if h:
        h = _expandir_usuario(h)
        candidato = h if os.path.isabs(h) else os.path.join(BASE, h)
    else:
        candidato = os.path.join(BASE, "accounts", pid)
    candidato = os.path.abspath(candidato)
    administrados = (
        os.path.join(BASE, "accounts", pid),
        os.path.join(ACCOUNTS_USUARIO, pid),
    )
    oficiales = {
        "claude": os.path.join(HOME_USUARIO, ".claude"),
        "gpt": os.path.join(HOME_USUARIO, ".codex"),
        "gemini": os.path.join(HOME_USUARIO, ".gemini"),
        "antigravity": os.path.join(HOME_USUARIO, ".gemini", "antigravity-cli"),
    }
    permitidos = list(administrados)
    oficial = oficiales.get(p.get("provider"))
    if oficial:
        permitidos.append(oficial)
    for permitido in permitidos:
        if os.path.abspath(_expandir_usuario(permitido)) != candidato:
            continue
        seguro = _ruta_sin_enlaces(os.path.dirname(permitido), candidato)
        if seguro == os.path.realpath(permitido):
            return seguro
    raise ValueError("home de cuenta fuera de los directorios administrados")


def ruta_api_key(pid, p):
    """La clave API ocupa un archivo dedicado; nunca otra credencial del home."""
    home = home_de(pid, p)
    valor = p.get("api_key_file") or "api_key"
    crudo = _expandir_usuario(valor)
    candidato = crudo if os.path.isabs(crudo) else os.path.join(home, crudo)
    esperado = os.path.join(home, "api_key")
    segura = _ruta_sin_enlaces(home, candidato)
    return segura if segura == esperado else None


def home_purgable(pid, p):
    """Devuelve solo el directorio privado directo y canónico de ese id."""
    if not id_perfil_valido(pid):
        return False
    try:
        actual = os.path.realpath(home_de(pid, p))
    except ValueError:
        return None
    esperado = os.path.realpath(os.path.join(ACCOUNTS, pid))
    dentro = ruta_contenida(ACCOUNTS, actual)
    return actual if dentro is not None and actual == esperado else None


def purgar_home_cuenta(pid, p):
    """Vacía ``accounts/<id>`` usando solo capacidades descriptor-relativas.

    Conserva los directorios y entradas regulares vacíos: POSIX no permite
    ligar ``unlink``/``rmdir`` al descriptor ya validado y reabrir por nombre
    introduciría una carrera identidad→borrado. ``--purge`` inutiliza el
    contenido secreto sin prometer borrar nombres o contenedores.
    """
    esperado = home_purgable(pid, p)
    if not esperado:
        return False
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) \
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)

    def vaciar(dir_fd, dispositivo, presupuesto, profundidad=0):
        if profundidad > 32:
            raise OSError("home demasiado profundo para purgar")
        with os.scandir(dir_fd) as entradas:
            for entrada in entradas:
                presupuesto[0] -= 1
                if presupuesto[0] < 0:
                    raise OSError("home demasiado grande para purgar")
                previo = entrada.stat(follow_symlinks=False)
                if previo.st_uid != os.getuid() or previo.st_dev != dispositivo:
                    raise OSError("entrada de credenciales no privada")
                identidad = (previo.st_dev, previo.st_ino, previo.st_mode)
                if stat.S_ISDIR(previo.st_mode):
                    hijo_fd = os.open(entrada.name, flags, dir_fd=dir_fd)
                    try:
                        abierto = os.fstat(hijo_fd)
                        if ((abierto.st_dev, abierto.st_ino, abierto.st_mode)
                                != identidad):
                            raise OSError("directorio de credenciales cambio")
                        vaciar(hijo_fd, dispositivo, presupuesto, profundidad + 1)
                    finally:
                        os.close(hijo_fd)
                elif stat.S_ISREG(previo.st_mode):
                    _vaciar_regular_privado_at(dir_fd, entrada.name, previo)
                else:
                    # Symlinks, sockets, fifos y dispositivos se preservan y
                    # hacen fallar la purga; nunca se borran por nombre.
                    raise OSError("entrada de credenciales no regular")
        os.fsync(dir_fd)

    try:
        with _abrir_directorio_seguro(ACCOUNTS) as accounts_fd:
            cuenta_fd = os.open(pid, flags, dir_fd=accounts_fd)
            try:
                estado = os.fstat(cuenta_fd)
                if (not stat.S_ISDIR(estado.st_mode)
                        or estado.st_uid != os.getuid() or estado.st_mode & 0o077):
                    return False
                vaciar(cuenta_fd, estado.st_dev, [100_000])
            finally:
                os.close(cuenta_fd)
            os.fsync(accounts_fd)
        return True
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False


def entorno(pid, p):
    env = dict(os.environ)
    env["HOME"] = HOME_USUARIO
    env["PATH"] = os.pathsep.join([
        os.path.join(HOME_USUARIO, ".local", "bin"),
        "/usr/local/bin", "/usr/bin", "/bin",
    ])
    prov = p.get("provider")
    h = home_de(pid, p)
    # Nunca heredar credenciales/endpoints de la cuenta elegida manualmente en
    # otra capa de la terminal. Cada perfil empieza aislado y solo reinyecta lo
    # que declara en su configuracion privada.
    for nombre in (
        "CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CLI_HOME",
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL", "OPENAI_API_KEY", "CODEX_API_KEY",
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_GCA",
        "GEMINI_CLI_TRUST_WORKSPACE", "BROWSER", "PAGER", "GIT_PAGER",
        "EDITOR", "VISUAL", "NODE_OPTIONS", "PYTHONPATH", "PYTHONHOME",
        "BASH_ENV", "ENV", "SHELLOPTS", "GIT_SSH_COMMAND", "GIT_ASKPASS",
        "SSH_ASKPASS", "GIT_EXEC_PATH", "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT", "LD_PRELOAD", "LD_LIBRARY_PATH",
    ):
        env.pop(nombre, None)
    for nombre in tuple(env):
        if (nombre.startswith("DYLD_") or nombre.startswith("GIT_CONFIG_KEY_")
                or nombre.startswith("GIT_CONFIG_VALUE_")):
            env.pop(nombre, None)
    if prov == "claude":
        env["CLAUDE_CONFIG_DIR"] = h
    elif prov == "gpt":
        env["CODEX_HOME"] = h
    elif prov == "minimax":
        env["CLAUDE_CONFIG_DIR"] = h
        env["ANTHROPIC_BASE_URL"] = base_url_minimax(p)
        k = ruta_api_key(pid, p)
        if k and os.path.isfile(k) and not os.path.islink(k):
            clave = _leer_texto(k, "").strip()
            if clave:
                env["ANTHROPIC_AUTH_TOKEN"] = clave
        if "ANTHROPIC_AUTH_TOKEN" not in env:
            raise ValueError("la cuenta MiniMax no tiene una API key privada")
        m = p.get("model") or MINIMAX_MODELO
        for v in ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                  "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                  "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            env[v] = m
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env.setdefault("API_TIMEOUT_MS", "3000000")
    elif prov == "antigravity":
        pass
    elif prov == "gemini":
        env["GEMINI_CLI_HOME"] = h
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        k = ruta_api_key(pid, p)
        if p.get("auth") == "oauth":
            env["GOOGLE_GENAI_USE_GCA"] = "true"
        elif k and os.path.isfile(k) and not os.path.islink(k):
            clave = _leer_texto(k, "").strip()
            if clave:
                env["GEMINI_API_KEY"] = clave
        if p.get("auth") != "oauth" and "GEMINI_API_KEY" not in env:
            raise ValueError("la cuenta Gemini no tiene una API key privada")
    personalizadas = p.get("env") or {}
    if not isinstance(personalizadas, dict):
        raise ValueError("env del perfil debe ser un objeto")
    # Una blocklist no alcanza: NODE_OPTIONS, BROWSER, pagers y variables de
    # Git/SSH también pueden cargar código. Solo admitimos ajustes escalares que
    # no seleccionan ejecutables, loaders, rutas, credenciales ni endpoints.
    permitidas = {"API_TIMEOUT_MS", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "NO_COLOR"}
    for k, v in personalizadas.items():
        if not isinstance(k, str) or k not in permitidas:
            raise ValueError("variable de entorno no permitida en el perfil")
        if not isinstance(v, (str, int, float, bool)):
            raise ValueError("valor de entorno no escalar en el perfil")
        env[k] = str(v)
    # Todo `git push` iniciado por una IA hereda un pre-push que escanea cada
    # commit. La publicacion normal de Orquesta vuelve a escanear por su cuenta;
    # esta capa evita que un modelo se salte accidentalmente el gate.
    hook = os.path.join(BASE, "tools", "git-hooks")
    if os.path.isfile(os.path.join(hook, "pre-push")):
        for nombre in tuple(env):
            if (nombre == "GIT_CONFIG_PARAMETERS" or nombre == "GIT_CONFIG_COUNT"
                    or nombre.startswith("GIT_CONFIG_KEY_")
                    or nombre.startswith("GIT_CONFIG_VALUE_")):
                env.pop(nombre, None)
        env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
        env["GIT_CONFIG_VALUE_0"] = hook
        env["GIT_CONFIG_COUNT"] = "1"
        env["ORQ_HOME"] = BASE
    return env


# Maxima potencia: modelo y esfuerzo mas altos de cada proveedor.
POTENCIA_MAX = {
    "claude": {"model": "claude-opus-5", "effort": "xhigh"},
    "gpt": {"reasoning": "high"},
    "antigravity": {"model": "gemini-3.1-pro-high", "effort": "high"},
    "gemini": {},
    # sin --effort: el flag es de la CLI de Anthropic, MiniMax no lo negocia
    "minimax": {"model": MINIMAX_MODELO},
}


def potencia_maxima():
    return cfg().get("_potencia_maxima", True)


PERMISOS = {
    "claude": ["--dangerously-skip-permissions"],
    "gpt": ["--dangerously-bypass-approvals-and-sandbox"],
    "antigravity": ["--dangerously-skip-permissions"],
    "gemini": ["--yolo"],
    "minimax": ["--dangerously-skip-permissions"],
}


def permisos_activos():
    local = os.environ.get("ORQ_PERMISOS_TOTALES")
    if local not in (None, ""):
        return str(local).strip().lower() in {"1", "true", "yes", "on"}
    return cfg().get("_permisos_totales", False)


def chrome_claude_instalado():
    """Detecta la extension oficial sin leer datos ni sesiones del navegador."""
    extension_id = "fcoeoabgfenejglbffodgkkbkcdhcgfn"
    patrones = [
        os.path.join(
            HOME_USUARIO, ".config", "google-chrome", "*", "Extensions",
            extension_id, "*", "manifest.json",
        ),
        os.path.join(
            HOME_USUARIO, ".config", "chromium", "*", "Extensions",
            extension_id, "*", "manifest.json",
        ),
    ]
    return any(glob.glob(patron) for patron in patrones)


def chrome_perfil_habilitado(p):
    valor = p.get("chrome")
    if valor is None:
        valor = os.environ.get("ORQ_CLAUDE_CHROME", "0")
    if str(valor).strip().lower() == "auto":
        return chrome_claude_instalado()
    return str(valor).strip().lower() in {"1", "true", "yes", "on"}


def comando(p, prompt, session_id=None, resume=False, solo_lectura=False):
    prov = p.get("provider")
    perm = PERMISOS.get(prov, []) if permisos_activos() and not solo_lectura else []
    mx = POTENCIA_MAX.get(prov, {}) if potencia_maxima() else {}
    # el perfil manda sobre el ajuste global
    modelo = p.get("model") or mx.get("model")
    if prov == "claude":
        base = ["claude", "-p", "--output-format", "json"] + perm
        if solo_lectura:
            base += ["--permission-mode", "plan"]
        if chrome_perfil_habilitado(p):
            base += ["--chrome"]
        if resume and session_id:
            base += ["--resume", session_id]
        elif session_id:
            base += ["--session-id", session_id]
        if modelo:
            base += ["--model", modelo]
        if mx.get("effort"):
            base += ["--effort", mx["effort"]]
        return base + ["--", prompt]
    if prov == "minimax":
        base = ["claude", "-p", "--output-format", "json"] + perm
        if solo_lectura:
            base += ["--permission-mode", "plan"]
        base += ["--model", modelo or MINIMAX_MODELO]
        return base + ["--", prompt]
    if prov == "gpt":
        base = ["codex", "exec", "--skip-git-repo-check"] + perm
        if solo_lectura:
            base += ["--sandbox", "read-only"]
        if modelo:
            base += ["-m", modelo]
        if mx.get("reasoning"):
            base += ["-c", f'model_reasoning_effort="{mx["reasoning"]}"']
        return base + ["--", prompt]
    if prov == "antigravity":
        base = ["agy", "--output-format", "json"] + perm
        if modelo:
            base += ["--model", modelo]
        if mx.get("effort"):
            base += ["--effort", mx["effort"]]
        return base + ["-p", prompt]
    if prov == "gemini":
        base = ["gemini", "--skip-trust"] + perm
        if p.get("model"):
            base += ["-m", p["model"]]
        return base + ["-p", prompt]
    raise ValueError(f"proveedor desconocido: {prov}")


def autenticado(pid, p):
    prov = p.get("provider")
    h = home_de(pid, p)

    def privada(ruta, no_vacia=False):
        if not ruta:
            return False
        try:
            with _abrir_regular(ruta, privado=True) as archivo:
                return bool(archivo.read(1)) if no_vacia else True
        except OSError:
            return False

    if prov == "claude":
        credencial = ruta_cuenta(pid, p, predeterminado=".credentials.json")
        return privada(credencial)
    if prov == "gpt":
        credencial = ruta_cuenta(pid, p, predeterminado="auth.json")
        return privada(credencial)
    if prov == "minimax":
        k = ruta_api_key(pid, p)
        return privada(k, no_vacia=True)
    if prov == "antigravity":
        import shutil
        return shutil.which("agy") is not None
    if prov == "gemini":
        if p.get("auth") == "oauth":
            credencial = ruta_cuenta(
                pid, p, predeterminado=os.path.join(".gemini", "oauth_creds.json")
            )
            return privada(credencial)
        k = ruta_api_key(pid, p)
        if privada(k, no_vacia=True):
            return True
        ajustes = ruta_cuenta(
            pid, p, predeterminado=os.path.join(".gemini", "settings.json")
        )
        return privada(ajustes)
    return False


def cmd_login(pid, p):
    """Comando exacto que el usuario debe correr para autenticar la cuenta."""
    prov = p.get("provider")
    h = home_de(pid, p)
    nav = p.get("navegador")
    pre = f"BROWSER={shlex.quote(nav)} " if nav else ""
    hq = shlex.quote(h)
    if prov == "claude":
        return f'{pre}CLAUDE_CONFIG_DIR={hq} claude   # dentro escribe: /login'
    if prov == "gpt":
        return f'{pre}CODEX_HOME={hq} codex login'
    if prov == "minimax":
        return (f'orq cuenta key {shlex.quote(pid)}'
                '   # pega la API key de platform.minimax.io (no se ve al escribir)')
    if prov == "antigravity":
        return "agy   # si pide sesion, autoriza en el navegador"
    if prov == "gemini":
        if p.get("auth") == "oauth":
            return (f'GEMINI_CLI_HOME={hq} GOOGLE_GENAI_USE_GCA=true '
                    f'BROWSER={shlex.quote(nav or "firefox")} gemini'
                    '   # autoriza con la cuenta de Google')
        k = shlex.quote(os.path.join(h, "api_key"))
        return (f'mkdir -p {hq} && printf %s TU_API_KEY > {k} '
                f'&& chmod 600 {k}')
    return "proveedor desconocido"


def guardar_api_key(pid, p, clave):
    """Guarda una clave sin devolver ni registrar ningún dato derivado de ella."""
    if not isinstance(clave, str) or len(clave.strip()) < 20:
        raise ValueError("clave invalida")
    home = home_de(pid, p)
    try:
        with _abrir_directorio_seguro(home, crear=True) as home_fd:
            os.fchmod(home_fd, 0o700)
    except OSError:
        raise OSError("no pude preparar el home privado") from None
    destino = ruta_api_key(pid, p)
    if destino is None or os.path.islink(destino):
        raise ValueError("api_key_file fuera del home privado")
    try:
        _escribir_texto(destino, clave.strip())
    except OSError:
        raise OSError("no pude guardar la clave de forma segura") from None


# ---------------- limites de uso ----------------
PAT_LIMITE = re.compile(
    r"(rate.?limit|usage limit|limit reached|too many requests|quota|"
    r"resource_exhausted|"
    r"limite de uso|has alcanzado)", re.I)
PAT_RESET = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)
PAT_RESET_EN = re.compile(
    r"resets?\s+in\s+(?:(\d+)\s*h(?:ours?)?)?\s*"
    r"(?:(\d+)\s*m(?:in(?:utes?)?)?)?\s*"
    r"(?:(\d+)\s*s(?:ec(?:onds?)?)?)?", re.I)


def detectar_limite(pid, p, texto):
    """Si la salida indica limite de uso, registra hasta cuando esta bloqueado."""
    if not texto or not PAT_LIMITE.search(texto):
        return None
    horas = p.get("ventana_horas") or VENTANA_PLAN.get(p.get("plan", "desconocido"), 5)
    hasta = ahora() + datetime.timedelta(hours=horas)
    relativo = PAT_RESET_EN.search(texto)
    if relativo and any(relativo.groups()):
        try:
            hh, mm, ss = (int(x or 0) for x in relativo.groups())
            hasta = ahora() + datetime.timedelta(hours=hh, minutes=mm, seconds=ss)
        except Exception:
            pass
    else:
        m = PAT_RESET.search(texto)
        if not m:
            m = None
    if not (relativo and any(relativo.groups())) and m:
        try:
            hh = int(m.group(1)); mm = int(m.group(2) or 0); ap = (m.group(3) or "").lower()
            if ap == "pm" and hh < 12: hh += 12
            if ap == "am" and hh == 12: hh = 0
            cand = ahora().replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand <= ahora():
                cand += datetime.timedelta(days=1)
            hasta = cand
        except Exception:
            pass
    with bloqueo():
        L = limites()
        clave = (CLAVE_LIMITE_ANTIGRAVITY
                 if p.get("provider") == "antigravity" else pid)
        L[clave] = {"bloqueado_hasta": hasta.isoformat(timespec="seconds"),
                    "detectado": ahora().isoformat(timespec="seconds"),
                    "motivo": texto.strip()[:200]}
        _escribir(LIMITS, L)
    return hasta


def bloqueado(pid):
    todos = limites()
    claves = [pid]
    perfiles = cfg().get("profiles", {})
    if (perfiles.get(pid) or {}).get("provider") == "antigravity":
        # Todos los perfiles AGY comparten la misma sesion y, por tanto, la
        # misma cuota. Un alias no puede eludir el limite de otro.
        claves = [CLAVE_LIMITE_ANTIGRAVITY] + [
            k for k, v in perfiles.items() if v.get("provider") == "antigravity"]
    vigentes = []
    for clave in claves:
        dato = todos.get(clave)
        if not dato:
            continue
        try:
            hasta = datetime.datetime.fromisoformat(dato["bloqueado_hasta"])
        except Exception:
            continue
        if hasta > ahora():
            vigentes.append(hasta)
    return max(vigentes) if vigentes else None


def limpiar_limite(pid):
    with bloqueo():
        L = limites()
        perfiles = cfg().get("profiles", {})
        if (perfiles.get(pid) or {}).get("provider") == "antigravity":
            L.pop(CLAVE_LIMITE_ANTIGRAVITY, None)
            for k, v in perfiles.items():
                if v.get("provider") == "antigravity":
                    L.pop(k, None)
        else:
            L.pop(pid, None)
        _escribir(LIMITS, L)


# ---------------- extraccion ----------------
def _texto_error(error):
    if not error:
        return ""
    if isinstance(error, str):
        return error.strip()
    if isinstance(error, dict):
        partes = []
        for k in ("status", "code", "message", "detail", "error"):
            v = error.get(k)
            if v not in (None, ""):
                partes.append(str(v))
        return " · ".join(dict.fromkeys(partes))
    return str(error).strip()


def extraer(provider, stdout, stderr=""):
    """Devuelve (respuesta, tokens, diagnostico_del_proveedor)."""
    if provider in ("claude", "minimax"):
        try:
            d = json.loads(stdout)
            u = d.get("usage", {}) or {}
            tok = (u.get("input_tokens", 0) + u.get("output_tokens", 0)
                   + u.get("cache_read_input_tokens", 0)
                   + u.get("cache_creation_input_tokens", 0))
            texto = d.get("result") or ""
            diag = _texto_error(d.get("error"))
            if d.get("is_error") and not diag:
                diag = str(texto).strip()
            return texto, tok, diag
        except Exception:
            return (stdout or "").strip(), 0, (stderr or "").strip()
    if provider == "antigravity":
        try:
            d = json.loads(stdout)
            u = d.get("usage", {}) or {}
            texto = (d.get("response") or "").strip()
            diag = _texto_error(d.get("error"))
            if str(d.get("status", "")).upper() == "ERROR" and not diag:
                diag = "Antigravity termino con estado ERROR"
            return texto, u.get("total_tokens", 0), diag
        except Exception:
            return (stdout or "").strip(), 0, (stderr or "").strip()
    if provider == "gpt":
        m = re.findall(r"tokens used\s*\n\s*([\d.,\s]*\d)", stderr or "")
        tok = sum(int(re.sub(r"\D", "", x)) for x in m if re.sub(r"\D", "", x))
        return (stdout or "").strip(), tok, (stderr or "").strip()
    return (stdout or "").strip(), 0, (stderr or "").strip()


def _error_estructurado(provider, stdout):
    """Distingue un error JSON de avisos normales escritos en stderr."""
    if provider not in ("claude", "antigravity", "minimax"):
        return False
    try:
        d = json.loads(stdout)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(d, dict):
        return False
    if provider in ("claude", "minimax"):
        return bool(d.get("is_error") or d.get("error"))
    return bool(d.get("error") or str(d.get("status", "")).upper() == "ERROR")


# ---------------- ejecucion ----------------
def _descendientes(pid_raiz):
    """Obtiene descendientes Linux aun si crearon otra sesion con setsid."""
    hijos = {}
    try:
        entradas = os.listdir("/proc")
    except OSError:
        return []
    for nombre in entradas:
        if not nombre.isdigit():
            continue
        try:
            with open(f"/proc/{nombre}/status") as estado:
                ppid = next(
                    int(linea.split()[1]) for linea in estado
                    if linea.startswith("PPid:")
                )
            hijos.setdefault(ppid, []).append(int(nombre))
        except (OSError, StopIteration, ValueError):
            continue
    salida, pila = [], [int(pid_raiz)]
    while pila:
        padre = pila.pop()
        nuevos = hijos.get(padre, [])
        salida.extend(nuevos)
        pila.extend(nuevos)
    return salida


def _terminar_grupo(proceso, gracia=0.5):
    """Detiene la sesion del proveedor completa, incluidos sus hijos.

    Cada proveedor se inicia como lider de una sesion nueva. En un timeout no
    basta con matar ese lider: las herramientas que lanzo pueden seguir
    modificando archivos y conservar abiertos stdout/stderr. Primero les damos
    una oportunidad breve de cerrar con TERM y despues eliminamos cualquier
    miembro restante del grupo con KILL.
    """
    pgid = proceso.pid
    descendientes = _descendientes(proceso.pid)

    def enviar_pids(sig, pids):
        for pid in reversed(pids):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass

    def enviar(sig):
        try:
            os.killpg(pgid, sig)
            return True
        except ProcessLookupError:
            return False

    enviar_pids(signal.SIGTERM, descendientes)
    enviar(signal.SIGTERM)
    limite = time.monotonic() + gracia
    grupo_vivo = True
    while time.monotonic() < limite:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            grupo_vivo = False
            break
        time.sleep(0.02)
    # Un hijo puede haber salido del grupo con setsid aunque el lider ya haya
    # muerto. Se elimina explicitamente usando la foto tomada antes del TERM.
    enviar_pids(signal.SIGKILL, list(dict.fromkeys(
        descendientes + _descendientes(proceso.pid)
    )))
    if grupo_vivo:
        enviar(signal.SIGKILL)

    # Recolectamos al lider para que no quede zombie. El KILL directo es un
    # ultimo respaldo si el grupo cambio de forma inesperada.
    try:
        proceso.wait(timeout=gracia)
    except subprocess.TimeoutExpired:
        proceso.kill()
        try:
            proceso.wait(timeout=gracia)
        except subprocess.TimeoutExpired:
            pass


def _systemd_usuario_disponible():
    """Comprueba el bus de usuario, no solo la presencia de sus binarios.

    En SSH, WSL y contenedores es comun tener ``systemd-run`` instalado sin
    una sesion de usuario utilizable. En ese caso envolver el proveedor hace
    que falle antes de llegar a ejecutarse. La cache se separa por las
    variables del bus y caduca pronto para tolerar que aparezca una sesion.
    """
    if (os.environ.get("ORQ_DISABLE_SYSTEMD_SCOPE")
            or not os.access("/usr/bin/systemd-run", os.X_OK)
            or not os.access("/usr/bin/systemctl", os.X_OK)):
        return False
    clave = (os.getuid(), os.environ.get("XDG_RUNTIME_DIR", ""),
             os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""))
    ahora_mono = time.monotonic()
    guardado = _SYSTEMD_USUARIO_CACHE.get(clave)
    if guardado and ahora_mono - guardado[0] < 15:
        return guardado[1]
    try:
        r = _SUBPROCESS_RUN_ORIGINAL(
            ["/usr/bin/systemctl", "--user", "show-environment"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=2, check=False,
        )
        disponible = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        disponible = False
    _SYSTEMD_USUARIO_CACHE[clave] = (ahora_mono, disponible)
    return disponible


def _scope_systemd(cmd):
    """Encierra un proveedor en un cgroup solo con bus de usuario operativo."""
    if not _systemd_usuario_disponible():
        return cmd, None
    unidad = f"orq-run-{os.getpid()}-{time.time_ns()}"
    return (["/usr/bin/systemd-run", "--user", "--scope", "--quiet",
             f"--unit={unidad}", "--", *cmd], unidad + ".scope")


def _terminar_aislado(proceso, scope=None, gracia=0.7):
    """Mata el cgroup completo; usa el grupo POSIX como respaldo portable."""
    if scope:
        def matar(senal):
            _SUBPROCESS_RUN_ORIGINAL(
                ["/usr/bin/systemctl", "--user", "kill", "--kill-whom=all",
                 f"--signal={senal}", scope],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=2, check=False,
            )
        try:
            matar("TERM")
            try:
                proceso.wait(timeout=gracia)
            except subprocess.TimeoutExpired:
                matar("KILL")
        except (OSError, subprocess.SubprocessError):
            pass
    _terminar_grupo(proceso, gracia=gracia)


def _salida_tras_interrupcion(proceso, espera=1.0):
    """Recoge la salida ya producida sin poder quedar bloqueado por un pipe."""
    try:
        return proceso.communicate(timeout=espera)
    except subprocess.TimeoutExpired as e:
        # Un descendiente que se desacoplo pudo heredar los pipes. El grupo
        # original ya esta muerto; conservamos la salida parcial y los cerramos.
        out, err = e.stdout or "", e.stderr or ""
        for pipe in (proceso.stdout, proceso.stderr):
            if pipe:
                try:
                    pipe.close()
                except OSError:
                    pass
        return out, err


def _modo_supervisor(cmd):
    """Reduce el ejecutable a un identificador de una lista cerrada."""
    if not isinstance(cmd, (list, tuple)) or not cmd or not isinstance(cmd[0], str):
        return None
    programa = os.path.basename(cmd[0])
    if programa == "claude":
        return "claude"
    if programa == "codex":
        return "codex"
    if programa == "agy":
        return "agy"
    if programa == "gemini":
        return "gemini"
    if programa == "git":
        opciones = list(cmd[1:])
        if opciones[:len(_GIT_INTERNO)] == _GIT_INTERNO:
            return "git-internal"
        if opciones[:len(_GIT_VERIFICACION)] == _GIT_VERIFICACION:
            return "git-verification"
        return None
    if programa in ("python", "python3"):
        fixture = os.path.join(CODIGO_ORQUESTA, "orqrun.py")
        if (len(cmd) == 4 and os.path.realpath(cmd[1]) == fixture
                and cmd[2] == "--fixture"
                and cmd[3] in {"infinite", "cwd-probe", "tree-parent", "setsid-parent",
                               "leader-exit"}):
            return "fixture"
        if len(cmd) >= 3 and cmd[1] == "-m" and cmd[2] in {
                "unittest", "pytest", "ruff", "mypy"}:
            return "python-module"
        return None
    if programa in ("pytest", "py.test"):
        return "pytest"
    if programa == "mypy":
        return "mypy"
    if programa == "ruff":
        return "ruff"
    if programa == "eslint":
        return "eslint"
    if programa == "node":
        return "node"
    if programa == "npm":
        return "npm"
    if programa == "pnpm":
        return "pnpm"
    if programa == "yarn":
        return "yarn"
    if programa == "cargo":
        return "cargo"
    if programa == "go":
        return "go"
    if programa == "make":
        return "make"
    if programa == "tsc":
        return "tsc"
    if programa == "systemd-analyze":
        return "systemd-analyze"
    if programa == "pwd" and len(cmd) == 1:
        return "pwd"
    if programa == "ls" and list(cmd[1:]) == ["-la"]:
        return "ls"
    if programa == "bash":
        scanner = os.path.join(CODIGO_ORQUESTA, "tools", "scan-secretos.sh")
        if len(cmd) > 1 and os.path.realpath(cmd[1]) == os.path.realpath(scanner):
            return "scanner"
        if len(cmd) > 1 and cmd[1] == "-n":
            return "bash-n"
        return None
    if programa == "sh" and len(cmd) > 1 and cmd[1] == "-n":
        return "sh-n"
    return None


def _ejecutar_aislado_acotado(cmd, env, cwd, timeout,
                              limite_salida=16 * 1024 * 1024,
                              limite_error=4 * 1024 * 1024,
                              usar_scope=True, cwd_fd=None):
    """Drena pipes con presupuestos duros y mata el árbol al excederlos."""
    proceso = None
    scope = None
    lectores = []
    lectores_iniciados = []
    excedido = threading.Event()
    buffers = {"out": bytearray(), "err": bytearray()}
    modo = _modo_supervisor(cmd)
    if modo is None:
        return "", "ejecutable fuera de la capacidad del supervisor", 126

    def drenar(pipe, nombre, limite):
        try:
            while True:
                bloque = pipe.read(64 * 1024)
                if not bloque:
                    return
                restante = limite - len(buffers[nombre])
                if restante > 0:
                    buffers[nombre].extend(bloque[:restante])
                if len(bloque) > restante:
                    excedido.set()
                    return
        except (OSError, ValueError):
            return

    try:
        # El supervisor fijo permanece vivo aunque el objetivo salga antes que
        # un daemon setsid. Como subreaper dedicado conserva esos huérfanos en
        # un árbol terminable sin alterar al proceso multihilo de Orquesta.
        supervisado = ["/usr/bin/python3", "-I", "-S",
                       os.path.join(CODIGO_ORQUESTA, "orqrun.py"),
                       "--mode", modo, *cmd[1:]]
        cmd_aislado, scope = (_scope_systemd(supervisado)
                              if usar_scope else (supervisado, None))
        with contextlib.ExitStack() as pila:
            if cwd_fd is None:
                _, trabajo_fd = pila.enter_context(_capacidad_trabajo(cwd))
            else:
                trabajo_fd = os.dup(cwd_fd)
                pila.callback(os.close, trabajo_fd)
                if not stat.S_ISDIR(os.fstat(trabajo_fd).st_mode):
                    raise OSError("capacidad cwd invalida")
            # ``/proc/self`` se evalúa en el hijo antes de cerrar descriptores;
            # pass_fds conserva la capacidad durante ese chdir. El objetivo
            # hereda después el cwd (el inode), no el nombre reabrible.
            cwd_capacidad = f"/proc/self/fd/{trabajo_fd}"
            proceso = subprocess.Popen(
                cmd_aislado, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, cwd=cwd_capacidad, stdin=subprocess.DEVNULL,
                start_new_session=True, pass_fds=(trabajo_fd,),
            )
        lectores = [
            threading.Thread(target=drenar, daemon=True,
                             args=(proceso.stdout, "out", limite_salida)),
            threading.Thread(target=drenar, daemon=True,
                             args=(proceso.stderr, "err", limite_error)),
        ]
        for lector in lectores:
            lector.start()
            lectores_iniciados.append(lector)
        fin = time.monotonic() + max(0.01, float(timeout))
        agotado_tiempo = False
        while proceso.poll() is None:
            if excedido.is_set():
                break
            if time.monotonic() >= fin:
                agotado_tiempo = True
                break
            time.sleep(0.02)
        if proceso.poll() is None:
            _terminar_aislado(proceso, scope)
        rc = 124 if agotado_tiempo else (125 if excedido.is_set()
                                         else proceso.returncode)
        for lector in lectores_iniciados:
            lector.join(1.0)
        if any(lector.is_alive() for lector in lectores_iniciados):
            # Un descendiente desacoplado conservó los pipes. Se cierra el scope
            # y los descriptores; no se espera ni se acumula salida sin límite.
            _terminar_aislado(proceso, scope)
            for pipe in (proceso.stdout, proceso.stderr):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass
            excedido.set()
            rc = 125 if rc == 0 else rc
        for lector in lectores_iniciados:
            lector.join(0.2)
    except KeyboardInterrupt:
        if proceso is not None:
            _terminar_aislado(proceso, scope)
            for pipe in (proceso.stdout, proceso.stderr):
                if pipe:
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        pass
        for lector in lectores_iniciados:
            lector.join(0.2)
        raise
    except FileNotFoundError as exc:
        buffers["err"].extend(
            f"binario no encontrado: {exc}".encode("utf-8", "replace")[:limite_error]
        )
        rc = 127
    except Exception as exc:
        # Desde el primer Popen cualquier fallo auxiliar (incluido el arranque
        # de un lector) debe terminar y recoger el supervisor antes de volver.
        # Nunca dejamos un objetivo escribiendo sin que alguien drene sus pipes.
        if proceso is not None:
            try:
                _terminar_aislado(proceso, scope)
            except (OSError, subprocess.SubprocessError):
                try:
                    proceso.kill()
                    proceso.wait(timeout=1)
                except (OSError, subprocess.SubprocessError):
                    pass
            for pipe in (proceso.stdout, proceso.stderr):
                if pipe:
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        pass
        for lector in lectores_iniciados:
            lector.join(0.5)
        buffers["err"].extend(str(exc).encode("utf-8", "replace")[:limite_error])
        rc = 127
    # Los lectores ya drenaron hasta EOF. Cerrar explícitamente evita dejar
    # descriptores y advertencias ResourceWarning en procesos de larga vida.
    for lector in lectores_iniciados:
        lector.join(0.2)
    for pipe in (() if proceso is None else (proceso.stdout, proceso.stderr)):
        if pipe:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
    out = bytes(buffers["out"]).decode("utf-8", "replace")
    err = bytes(buffers["err"]).decode("utf-8", "replace")
    if rc == 124:
        err = (err.rstrip() + f"\ntimeout tras {timeout}s").strip()
    if excedido.is_set():
        err = (err.rstrip() + "\nsalida excedio el limite seguro").strip()
        if rc == 0:
            rc = 125
    return out, err, rc


def _fallo_previo(pid, p, prompt, tarea, carpeta, detalle):
    run_id = f"{pid}-{time.time_ns()}"
    texto = f"[ERROR capacidad] {detalle}"
    log({"ts": ahora().isoformat(timespec="seconds"), "fecha": hoy(),
         "semana": ahora().strftime("%G-S%V"), "mes": ahora().strftime("%Y-%m"),
         "perfil": pid, "provider": p.get("provider", "?"), "tarea": tarea,
         "tokens": 0, "seg": 0.0, "rc": 2, "limite": False,
         "sesion": os.environ.get("ORQ_SESION", "sin-sesion"),
         "term": os.environ.get("ORQ_SESION_TERM", ""),
         "carpeta": carpeta or "", "prompt": prompt[:200], "run_id": run_id})
    return {"perfil": pid, "label": p.get("label", pid), "texto": texto,
            "tokens": 0, "seg": 0.0, "rc": 2, "run_id": run_id,
            "limitado": None}


def correr(pid, p, prompt, tarea="reasoning", timeout=300, carpeta=None,
           session_id=None, resume=False, solo_lectura=False, cwd_fd=None):
    if not admite_tarea(p, tarea):
        return _fallo_previo(
            pid, p, prompt, tarea, carpeta,
            f"{pid} ({p.get('provider', '?')}) no admite la tarea '{tarea}'",
        )
    if cwd_fd is not None:
        try:
            estado_cwd = os.fstat(cwd_fd)
            destino = carpeta or "."
            if not stat.S_ISDIR(estado_cwd.st_mode):
                destino = None
        except OSError:
            destino = None
    else:
        destino = BASE if carpeta is None else ruta_trabajo_segura(carpeta)
    if not destino:
        return _fallo_previo(
            pid, p, prompt, tarea, carpeta,
            "la carpeta solicitada esta fuera de las areas autorizadas",
        )

    prov = p.get("provider")
    if prov == "claude" and not session_id:
        session_id = str(uuid.uuid4())
    env = entorno(pid, p)
    opciones_comando = {"solo_lectura": True} if solo_lectura else {}
    cmd = comando(p, prompt, session_id=session_id, resume=resume,
                  **opciones_comando)
    t0 = time.time()
    if prov == "claude":
        # Claude admite cuentas distintas en paralelo, pero dos procesos sobre
        # el mismo CLAUDE_CONFIG_DIR pueden competir por su estado local. El
        # path determinista permite que otros procesos locales (por ejemplo el
        # motor editorial) respeten exactamente el mismo candado sin importar
        # este modulo ni leer profiles.json.
        f_lock = _lock_cuenta_claude(pid, p)
    else:
        f_lock = _lock_proveedor(prov) if prov in SERIALIZAR else None
    proceso = None
    scope = None
    try:
        if f_lock:
            fcntl.flock(f_lock, fcntl.LOCK_EX)
        if subprocess.run is not _SUBPROCESS_RUN_ORIGINAL:
            # Punto de inyeccion conservado para consumidores que sustituian
            # el runner (incluida la suite historica) sin arrancar una IA real.
            with (_capacidad_trabajo(destino) if cwd_fd is None
                  else contextlib.nullcontext((destino, cwd_fd))) as (_, ejec_fd):
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout,
                    env=env, cwd=f"/proc/self/fd/{ejec_fd}",
                    pass_fds=(ejec_fd,),
                )
            out, err, rc = r.stdout, r.stderr, r.returncode
        else:
            out, err, rc = _ejecutar_aislado_acotado(
                cmd, env, destino, timeout, cwd_fd=cwd_fd
            )
    except subprocess.TimeoutExpired as e:
        if proceso is not None:
            _terminar_aislado(proceso, scope)
            out, err = _salida_tras_interrupcion(proceso)
        else:
            out, err = e.stdout or "", e.stderr or ""
        # ``communicate`` reintentado entrega toda la salida acumulada. Si un
        # pipe desacoplado impidio recogerla, usamos lo que traia el timeout.
        out = out or e.stdout or ""
        err = err or e.stderr or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        err = (err.rstrip() + f"\ntimeout tras {timeout}s").strip()
        rc = 124
    except KeyboardInterrupt:
        if proceso is not None:
            _terminar_aislado(proceso, scope)
            _salida_tras_interrupcion(proceso)
        raise
    except FileNotFoundError as e:
        out, err, rc = "", f"binario no encontrado: {e}", 127
    finally:
        if f_lock:
            try:
                fcntl.flock(f_lock, fcntl.LOCK_UN)
            finally:
                f_lock.close()
    dur = round(time.time() - t0, 1)
    texto, tok, diagnostico = extraer(p["provider"], out, err)
    error_estructurado = _error_estructurado(p["provider"], out)
    if rc != 0 and not diagnostico:
        diagnostico = ((out or "") + "\n" + (err or "")).strip()
    if rc == 0 and (error_estructurado or not (texto or "").strip()):
        rc = 1
        diagnostico = diagnostico or "el proveedor termino sin respuesta"
    # Stderr de Codex puede contener el texto normal del trabajo (incluidos
    # numeros o la palabra quota). Solo es evidencia de limite si la llamada
    # fallo o el JSON del proveedor declara explicitamente un error.
    lim = detectar_limite(pid, p, diagnostico) if (rc != 0 or error_estructurado) else None
    if rc != 0:
        detalle = "\n".join(x for x in (diagnostico, texto) if x).strip()
        detalle = detalle or "sin detalle del proveedor"
        texto = f"[ERROR rc={rc}] {detalle.strip()[:400]}"
    run_id = f"{pid}-{time.time_ns()}"
    log({"ts": ahora().isoformat(timespec="seconds"), "fecha": hoy(),
         "semana": ahora().strftime("%G-S%V"), "mes": ahora().strftime("%Y-%m"),
         "perfil": pid, "provider": p["provider"], "tarea": tarea,
         "tokens": tok, "seg": dur, "rc": rc, "limite": bool(lim),
         "sesion": os.environ.get("ORQ_SESION", "sin-sesion"),
         "term": os.environ.get("ORQ_SESION_TERM", ""),
         "carpeta": carpeta or "", "prompt": prompt[:200], "run_id": run_id,
         "session_id": session_id})
    return {"perfil": pid, "label": p.get("label", pid), "texto": texto,
            "tokens": tok, "seg": dur, "rc": rc, "run_id": run_id,
            "limitado": lim.isoformat(timespec="seconds") if lim else None,
            "session_id": session_id}


# ---------------- routing ----------------
def disponibles(incluir_bloqueados=False, tarea=None):
    out = {}
    globales = set()
    for pid, p in cfg().get("profiles", {}).items():
        if not p.get("enabled", True):
            continue
        if tarea and not admite_tarea(p, tarea):
            continue
        if not autenticado(pid, p):
            continue
        if not incluir_bloqueados and bloqueado(pid):
            continue
        # AGY usa una sola sesion global. Perfiles adicionales serian alias de
        # la misma cuenta y falsearian el reparto y la cuota.
        if p.get("provider") == "antigravity":
            if "antigravity" in globales:
                continue
            globales.add("antigravity")
        out[pid] = p
    return out


def puntuar(pid, p, tarea):
    if not admite_tarea(p, tarea):
        return 0.0, ("solo imagen/diseno" if p.get("provider") == "antigravity"
                     else f"no admite {tarea}")
    base = potencia_perfil(p, tarea)
    s = scores().get(pid, {}).get(tarea)
    mult = 1.0
    if s and s.get("n"):
        mult = 0.5 + (s["suma"] / s["n"]) / 10.0
    notas = []
    # holgura de presupuesto diario
    presup = p.get("budget_tokens_dia", 0)
    factor = 1.0
    if presup > 0:
        g = gastado_hoy(pid)
        if g >= presup:
            return 0.0, f"tope diario agotado ({g}/{presup})"
        factor *= 0.4 + 0.6 * (1 - g / presup)
        notas.append(f"dia {g}/{presup}")
    # holgura de la ventana de recarga
    horas = p.get("ventana_horas") or VENTANA_PLAN.get(p.get("plan", "desconocido"), 5)
    cupo = p.get("cupo_ventana", 0)
    if cupo > 0:
        gv = gastado_ventana(pid, horas)
        if gv >= cupo:
            return 0.0, f"ventana {horas}h agotada ({gv}/{cupo})"
        factor *= 0.3 + 0.7 * (1 - gv / cupo)
        notas.append(f"ventana{horas}h {gv}/{cupo}")
    # Cuota REAL del proveedor cuando existe (codex la publica).
    try:
        q = cuota(pid, p)
    except Exception as e:
        q = {}
        if os.environ.get("ORQ_DEBUG"):
            print(f"[orq] cuota({pid}) fallo: {e!r}", file=sys.stderr)
    pct = q.get("usado_pct")
    g = ({"pct": pct, "fuente": "proveedor",
          "reinicia": q.get("reinicia"), "ventana_min": q.get("ventana_min")}
         if q.get("fuente") == "proveedor" and pct is not None
         else cuota_global(pid, p))
    # Un dato oficial o declarado es global y manda sobre la estimacion local,
    # que solo ve las sesiones de esta maquina.
    if (g.get("fuente") in ("proveedor", "declarado")
            and g.get("pct") is not None):
        pct = g["pct"]
        q = dict(q)
        q.update({"fuente": g["fuente"], "reinicia": g.get("reinicia"),
                  "ventana_min": (g.get("ventana_min")
                                   or q.get("ventana_min")
                                   or (g.get("ventana_h") or 0) * 60
                                   or (p.get("ventana_horas") or 5) * 60)})
    if pct is not None and q.get("fuente") in ("proveedor", "declarado", "local"):
        umbral = 100 if q.get("fuente") == "local" else 97
        if pct >= umbral:
            return 0.0, f"cuota practicamente agotada ({pct:.0f}%)"
        # Penalizacion proporcionada: gastar el 82% de una ventana SEMANAL que
        # recarga manana no es lo mismo que agotar una de 5 horas.
        castigo = 0.55 * (pct / 100) ** 1.25
        horas_ventana = (q.get("ventana_min") or 300) / 60
        if horas_ventana >= 24:
            castigo *= 0.75            # ventanas largas se toleran mejor
        try:
            if q.get("reinicia"):
                falta = (datetime.datetime.fromisoformat(q["reinicia"]) - ahora())
                if falta.total_seconds() < 36 * 3600:
                    castigo *= 0.7     # recarga inminente: casi no penalizar
        except Exception:
            pass
        factor *= max(0.40, 1 - castigo)
        notas.append(f"{pct:.0f}% de su cuota"
                     + (f", recarga {q['reinicia'][5:16]}" if q.get("reinicia") else ""))
    elif q.get("fuente") == "local" and q.get("mensajes"):
        notas.append(f"{q['facturable']:,} tok reales en {q.get('ventana_horas',5)}h")

    # Equilibrio entre cuentas del MISMO proveedor: sin saber el limite exacto
    # del plan, reparte segun quien ha consumido menos en su ventana.
    hermanas = [(k, v) for k, v in cfg().get("profiles", {}).items()
                if v.get("provider") == p.get("provider") and v.get("enabled", True)
                and autenticado(k, v) and not bloqueado(k)]
    if len(hermanas) > 1:
        gastos = {}
        for k, v in hermanas:
            hv = v.get("ventana_horas") or VENTANA_PLAN.get(v.get("plan", "desconocido"), 5)
            try:
                # uso REAL (todas las sesiones), no solo lo que gasto Orquesta
                gastos[k] = uso_real_ventana(k, v, hv)["facturable"]
            except Exception:
                gastos[k] = gastado_ventana(k, hv)
            # normalizar por el plan: un Max 20x aguanta mas que un Max 5x
            mplan = ((plan_claude(k, v) or {}).get("multiplicador", 1)
                     if v.get("provider") == "claude" else 1)
            if mplan > 1:
                gastos[k] = gastos[k] / mplan
        total = sum(gastos.values())
        periodo_reparto = "ventana"
        if total < MIN_MUESTRA_REPARTO:
            # Una ventana recien recargada o con unas pocas llamadas de prueba
            # no debe producir un 100/0 artificial. El uso del dia da una
            # muestra estable y ayuda a repartir tambien limites semanales.
            for k, v in hermanas:
                try:
                    d = uso_real(k, v).get("dias", {}).get(hoy(), {})
                    gastos[k] = d.get("entrada", 0) + d.get("salida", 0)
                except Exception:
                    gastos[k] = gastado_hoy(k)
                mplan = ((plan_claude(k, v) or {}).get("multiplicador", 1)
                         if v.get("provider") == "claude" else 1)
                if mplan > 1:
                    gastos[k] = gastos[k] / mplan
            total = sum(gastos.values())
            periodo_reparto = "dia"
        if total > 0:
            parte = gastos.get(pid, 0) / total          # 0 = sin usar, 1 = se lo lleva todo
            justo = 1.0 / len(hermanas)
            # quien va por debajo de su parte justa sube; quien va por encima baja
            factor *= max(0.45, min(1.55, 1 + (justo - parte)))
            notas.append(f"reparto {periodo_reparto} {parte*100:.0f}% de "
                         f"{p.get('provider')}")
    if not notas:
        notas.append("sin tope")
    return base * mult * factor, " · ".join(notas)


def ranking(tarea, proposito=None, incluir_bloqueados=False, preferir=None):
    disp = disponibles(incluir_bloqueados, tarea=tarea)
    if proposito:
        f = {k: v for k, v in disp.items()
             if v.get("proposito") in (proposito, "general")}
        disp = f or disp
    out = []
    for pid, p in disp.items():
        pts, nota = puntuar(pid, p, tarea)
        out.append({"pid": pid, "p": p, "pts": pts, "nota": nota})
    out.sort(key=lambda x: -x["pts"])
    out = [x for x in out if x["pts"] > 0]
    if preferir:
        # ``orq usar`` expresa una preferencia explicita. Conservamos el
        # puntaje para el resto y solo adelantamos esa cuenta si es elegible.
        out.sort(key=lambda x: x["pid"] != preferir)
    else:
        # Las cuentas fijadas con ``orq usar`` son la eleccion del usuario, no
        # una mera variable para el CLI directo. Entre ellas se conserva el
        # orden por capacidad/cupo; las hermanas de reserva quedan despues.
        preferidas = set(activas().values())
        if preferidas:
            out.sort(key=lambda x: x["pid"] not in preferidas)
    return out

# ---------------- navegadores y terminales ----------------
NAVEGADORES = [("firefox", "Firefox"), ("brave", "Brave"),
               ("google-chrome", "Google Chrome"), ("chromium", "Chromium"),
               ("microsoft-edge", "Edge")]
TERMINALES = [
    ("kitty", ["kitty", "--title", "LOGIN · ORQUESTA", "-e"]),
    ("ptyxis", ["ptyxis", "--title", "LOGIN · ORQUESTA", "--"]),
    ("gnome-terminal", ["gnome-terminal", "--title", "LOGIN · ORQUESTA", "--"]),
    ("konsole", ["konsole", "-e"]),
    ("xterm", ["xterm", "-T", "LOGIN · ORQUESTA", "-e"]),
]


def navegadores():
    import shutil
    out, vistos = [], set()
    for b, n in NAVEGADORES:
        r = shutil.which(b)
        if r and n not in vistos:
            vistos.add(n)
            out.append({"bin": b, "nombre": n})
    return out


def navegador_valido(valor):
    return (valor in (None, "")
            or (isinstance(valor, str) and valor in {b for b, _ in NAVEGADORES}))


def terminal_disponible():
    ruta_sistema = "/usr/local/bin:/usr/bin:/bin"
    for t, plantilla in TERMINALES:
        ejecutable = shutil.which(t, path=ruta_sistema)
        if ejecutable:
            return t, [ejecutable, *plantilla[1:]]
    return None, None


def lanzar_login(pid, p, titulo=None):
    """Abre el autenticador con argv fijo, sin construir un programa de shell."""
    if not id_perfil_valido(pid):
        return False, "id de cuenta invalido"
    if p.get("provider") not in ("claude", "gpt", "antigravity", "gemini", "minimax"):
        return False, "proveedor no permitido"
    if not navegador_valido(p.get("navegador")):
        return False, "navegador no permitido"
    try:
        home_de(pid, p)
    except ValueError as e:
        return False, str(e)
    t, plantilla = terminal_disponible()
    if not t:
        return False, "no encontre ninguna terminal grafica instalada"
    args = list(plantilla) + ["/usr/bin/python3", "-I", "-S",
                              os.path.join(BASE, "orqlogin.py")]
    # El helper Python debe arrancar sin loaders, rutas de importación ni PATH
    # controlados por el proceso que llamó al panel. Solo preservamos los
    # descriptores imprescindibles de la sesión gráfica.
    env = {
        nombre: os.environ[nombre]
        for nombre in (
            "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY",
            "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "LANG", "LC_ALL",
            "LC_CTYPE", "TERM", "COLORTERM", "DESKTOP_SESSION",
            "XDG_CURRENT_DESKTOP",
        )
        if nombre in os.environ
    }
    env["HOME"] = HOME_USUARIO
    env["PATH"] = os.pathsep.join([
        os.path.join(HOME_USUARIO, ".local", "bin"),
        "/usr/local/bin", "/usr/bin", "/bin",
    ])
    env["ORQ_LOGIN_PROFILE"] = pid
    navegador = p.get("navegador")
    if navegador:
        env["BROWSER"] = navegador
    try:
        subprocess.Popen(args, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=env)
        return True, f"terminal abierta ({t})"
    except Exception as e:
        return False, str(e)

# ---------------- cuenta activa por proveedor ----------------
ENTORNO_SH = os.path.join(BASE, "state", "entorno.sh")


def activas():
    """{proveedor: id_de_cuenta} que usan por defecto TODAS las terminales."""
    return cfg().get("_activas", {})


def escribir_entorno():
    """Actualiza el marcador que hace recargar el entorno activo.

    El marcador nunca contiene ni ejecuta datos de perfiles. ``shell.sh`` llama
    al helper fijo ``orqenv.py``, recibe pares NUL-delimitados y solo asigna una
    lista cerrada de variables. Así no hay código de shell generado ni secretos
    persistidos por duplicado.
    """
    with bloqueo():
        _escribir_texto(ENTORNO_SH, "orquesta-entorno-v2\n")
    return ENTORNO_SH


def usar(pid):
    """Fija esa cuenta como la activa de su proveedor, para todas las terminales."""
    c = cfg()
    p = c.get("profiles", {}).get(pid)
    if not p:
        return False, f"cuenta desconocida: {pid}"
    if not autenticado(pid, p):
        return False, f"'{pid}' no esta autenticada todavia"
    with bloqueo():
        c = cfg()
        c.setdefault("_activas", {})[p["provider"]] = pid
        guardar_cfg(c)
    escribir_entorno()
    return True, f"'{pid}' es ahora la cuenta {p['provider']} de todas las terminales nuevas"

# ---------------- informes de uso ----------------
def _periodo_de(r, periodo):
    if periodo == "dia":
        return r.get("fecha", "")
    if periodo == "semana":
        return r.get("semana") or _sem_de_ts(r.get("ts", ""))
    if periodo == "mes":
        return r.get("mes") or (r.get("fecha", "")[:7])
    return "todo"


def _sem_de_ts(ts):
    try:
        return datetime.datetime.fromisoformat(ts).strftime("%G-S%V")
    except Exception:
        return ""


def uso(periodo="mes", agrupar="perfil", limite_periodos=6):
    """Agrega el ledger por periodo (dia/semana/mes) y por perfil, tarea o sesion."""
    rows = ledger_rows()
    out = {}
    for r in rows:
        per = _periodo_de(r, periodo)
        if not per:
            continue
        clave = r.get(agrupar) or "?"
        d = out.setdefault(per, {}).setdefault(clave, {
            "tokens": 0, "llamadas": 0, "seg": 0.0, "errores": 0, "term": r.get("term", "")})
        d["tokens"] += r.get("tokens", 0)
        d["llamadas"] += 1
        d["seg"] += r.get("seg", 0)
        if r.get("rc"):
            d["errores"] += 1
    periodos = sorted(out.keys(), reverse=True)[:limite_periodos]
    return {p: out[p] for p in periodos}


def resumen_uso():
    """Totales rapidos: hoy, esta semana, este mes, historico."""
    rows = ledger_rows()
    n = ahora()
    hoy_s, sem_s, mes_s = hoy(), n.strftime("%G-S%V"), n.strftime("%Y-%m")
    r = {"hoy": [0, 0], "semana": [0, 0], "mes": [0, 0], "total": [0, 0]}
    sesiones = set()
    for x in rows:
        t, c = x.get("tokens", 0), 1
        r["total"][0] += t; r["total"][1] += c
        if x.get("fecha") == hoy_s:
            r["hoy"][0] += t; r["hoy"][1] += c
        if (x.get("semana") or _sem_de_ts(x.get("ts", ""))) == sem_s:
            r["semana"][0] += t; r["semana"][1] += c
        if (x.get("mes") or x.get("fecha", "")[:7]) == mes_s:
            r["mes"][0] += t; r["mes"][1] += c
            sesiones.add(x.get("sesion", ""))
    return {k: {"tokens": v[0], "llamadas": v[1]} for k, v in r.items()} | \
           {"sesiones_mes": len([s for s in sesiones if s and s != "sin-sesion"])}

# ---------------- generacion de imagenes ----------------
SCRATCH_AGY = os.path.join(HOME_USUARIO, ".gemini", "antigravity-cli", "scratch")


def _imagenes_en(d):
    segura = ruta_trabajo_segura(d)
    if not segura:
        return {}
    return {
        ruta: mtime for mtime, ruta in _archivos_regulares(
            segura,
            lambda nombre: os.path.splitext(nombre)[1].lower()
            in (".png", ".jpg", ".jpeg", ".webp"),
            limite=1000,
            profundidad_maxima=0,
            con_identidad=True,
        )
    }


def extension_real(ruta, identidad=None):
    """Devuelve la extension segun el contenido, no segun el nombre."""
    try:
        with _abrir_regular_identidad(ruta, identidad, mode="rb") as f:
            cab = f.read(12)
    except Exception:
        return None
    if cab.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if cab.startswith(b"\x89PNG\r\n"):
        return "png"
    if cab[:4] == b"RIFF" and cab[8:12] == b"WEBP":
        return "webp"
    return None


def _generar_imagen_bloqueada(pid, p, prompt, destino=None, timeout=420):
    """Genera una imagen y la deja en 'destino' con la extension correcta."""
    if not admite_tarea(p, "imagen"):
        return {"perfil": pid, "texto": f"[ERROR capacidad] {pid} no genera imagenes",
                "tokens": 0, "seg": 0.0, "rc": 2, "run_id": "", "archivos": [],
                "origen": [], "limitado": None}
    destino = preparar_ruta_trabajo(destino or BASE, crear=True)
    if not destino:
        return {"perfil": pid, "texto": "[ERROR capacidad] destino no autorizado",
                "tokens": 0, "seg": 0.0, "rc": 2, "run_id": "", "archivos": [],
                "origen": [], "limitado": None}
    antes = _imagenes_en(SCRATCH_AGY)
    antes_dst = _imagenes_en(destino)
    env = entorno(pid, p)
    cmd = comando(p, prompt)
    t0 = time.time()
    proceso = None
    scope = None
    try:
        if subprocess.run is not _SUBPROCESS_RUN_ORIGINAL:
            with _capacidad_trabajo(destino) as (_, cwd_fd):
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout,
                    env=env, cwd=f"/proc/self/fd/{cwd_fd}",
                    pass_fds=(cwd_fd,),
                )
            out, err, rc = r.stdout, r.stderr, r.returncode
        else:
            out, err, rc = _ejecutar_aislado_acotado(
                cmd, env, destino, timeout
            )
    except subprocess.TimeoutExpired as e:
        if proceso is not None:
            _terminar_aislado(proceso, scope)
            out, err = _salida_tras_interrupcion(proceso)
        else:
            out, err = e.stdout or "", e.stderr or ""
        out = out or e.stdout or ""
        err = err or e.stderr or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        err = (err.rstrip() + f"\ntimeout tras {timeout}s").strip()
        rc = 124
    except KeyboardInterrupt:
        if proceso is not None:
            _terminar_aislado(proceso, scope)
            _salida_tras_interrupcion(proceso)
        raise
    except FileNotFoundError as e:
        out, err, rc = "", f"binario no encontrado: {e}", 127
    dur = round(time.time() - t0, 1)
    texto, tok, diagnostico = extraer(p["provider"], out, err)
    error_estructurado = _error_estructurado(p["provider"], out)
    if rc != 0 and not diagnostico:
        diagnostico = ((out or "") + "\n" + (err or "")).strip()
    if rc == 0 and (error_estructurado or not (texto or "").strip()):
        rc = 1
        diagnostico = diagnostico or "el proveedor termino sin respuesta"
    lim = detectar_limite(pid, p, diagnostico) if (rc != 0 or error_estructurado) else None
    if rc != 0:
        detalle = "\n".join(x for x in (diagnostico, texto) if x).strip()
        detalle = detalle or "sin detalle del proveedor"
        texto = f"[ERROR rc={rc}] {detalle.strip()[:400]}"

    # 1) lo que el modelo dejo directamente en el destino
    guardadas = []
    ahora_dst = _imagenes_en(destino)
    for f, firma in sorted(ahora_dst.items(), key=lambda x: -x[1][0]):
        if f not in antes_dst or firma != antes_dst.get(f):
            ext = extension_real(f, firma)
            if ext and not f.lower().endswith("." + ext):
                nuevo = os.path.splitext(f)[0] + "." + ext
                try:
                    f = _copiar_regular_unico(f, nuevo, firma)
                except OSError:
                    continue
            guardadas.append(f)
    # 2) si no, lo que quedo en el scratch de agy
    nuevas = _imagenes_en(SCRATCH_AGY)
    creadas = sorted([f for f, firma in nuevas.items()
                      if f not in antes or firma != antes.get(f)],
                     key=lambda f: nuevas[f][0], reverse=True)
    if creadas and not guardadas:
        for i, f in enumerate(creadas[:4]):
            firma = nuevas[f]
            ext = extension_real(f, firma) or "png"
            base = re.sub(
                r"[^A-Za-z0-9._-]+", "-", os.path.splitext(os.path.basename(f))[0]
            ).strip(".-_")[:64] or "imagen"
            dst = os.path.join(destino, f"{base}{'' if i == 0 else '-' + str(i)}.{ext}")
            dst = _copiar_regular_unico(f, dst, firma)
            guardadas.append(dst)

    run_id = f"{pid}-{time.time_ns()}"
    log({"ts": ahora().isoformat(timespec="seconds"), "fecha": hoy(),
         "semana": ahora().strftime("%G-S%V"), "mes": ahora().strftime("%Y-%m"),
         "perfil": pid, "provider": p["provider"], "tarea": "imagen",
         "tokens": tok, "seg": dur, "rc": rc, "limite": bool(lim),
         "sesion": os.environ.get("ORQ_SESION", "sin-sesion"),
         "term": os.environ.get("ORQ_SESION_TERM", ""),
         "prompt": prompt[:200], "run_id": run_id})
    return {"perfil": pid, "texto": texto, "tokens": tok, "seg": dur, "rc": rc,
            "run_id": run_id, "archivos": guardadas, "origen": creadas[:4],
            "limitado": lim.isoformat(timespec="seconds") if lim else None}


def generar_imagen(pid, p, prompt, destino=None, timeout=420):
    """Serializa snapshot, proveedor y copia porque AGY comparte un scratch."""
    lock = _lock_proveedor("antigravity")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _generar_imagen_bloqueada(pid, p, prompt, destino, timeout)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

# ---------------- sesiones externas y serializacion ----------------
# Los CLIs de suscripcion no toleran bien varias sesiones a la vez: si tienes
# un 'codex --yolo' trabajando en otra terminal, una llamada nuestra se encola.
PATRON_PROC = {"claude": r"(^|/)claude(\s|$)", "gpt": r"(^|/)codex(\s|$)",
               "antigravity": r"(^|/)agy(\s|$)"}
SERIALIZAR = {"gpt", "antigravity"}  # CLIs/estado global que exigen serialización


def sesiones_externas():
    """Procesos de CLI vivos que NO lanzo el orquestador (tus terminales)."""
    out = {}
    try:
        ps = subprocess.run(
            ["/usr/bin/ps", "-eo", "pid,etimes,args"],
            capture_output=True, text=True, timeout=8,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
        ).stdout
    except Exception:
        return out
    mio = str(os.getpid())
    for linea in ps.splitlines()[1:]:
        partes = linea.strip().split(None, 2)
        if len(partes) < 3:
            continue
        pid, seg, args = partes
        if pid == mio or "ps -eo" in args or "orqlib" in args:
            continue
        base = args.split()[0]
        for prov, pat in PATRON_PROC.items():
            if re.search(pat, base) and "-p " not in args and "exec" not in args:
                out.setdefault(prov, []).append(
                    {"pid": pid, "segundos": int(seg) if seg.isdigit() else 0,
                     "cmd": args[:70]})
    return out


def _lock_proveedor(prov):
    """Semaforo por proveedor para no pisar sesiones concurrentes."""
    ruta = os.path.join(BASE, "state", f".lock-{prov}")
    return _abrir_lock(ruta)


def ruta_lock_cuenta_claude(pid, p):
    """Ruta estable y opaca del candado asociado a un CLAUDE_CONFIG_DIR."""
    cuenta = os.path.realpath(home_de(pid, p))
    clave = hashlib.sha256(
        cuenta.encode("utf-8", "surrogateescape")
    ).hexdigest()
    return os.path.join(BASE, "state", "account-locks", f"claude-{clave}.lock")


def _lock_cuenta_claude(pid, p):
    ruta = ruta_lock_cuenta_claude(pid, p)
    return _abrir_lock(ruta)


@contextlib.contextmanager
def bloqueo_proyecto(carpeta):
    """Un solo proyecto escritor por carpeta, incluso desde otras terminales."""
    real = ruta_trabajo_segura(carpeta)
    if not real:
        raise ValueError("carpeta de proyecto no autorizada")
    repo = _git(real, "rev-parse", "--show-toplevel", timeout=5)
    if repo and repo.returncode == 0 and repo.stdout.strip():
        raiz_repo = ruta_trabajo_segura(repo.stdout.strip())
        if raiz_repo:
            real = raiz_repo
    clave = hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()
    directorio = os.path.join(BASE, "state", "project-locks")
    archivo = _abrir_lock(os.path.join(directorio, clave + ".lock"))
    try:
        try:
            fcntl.flock(archivo, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"[orq] otro proyecto esta escribiendo en {real}; esperando su cierre…",
                  file=sys.stderr, flush=True)
            fcntl.flock(archivo, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(archivo, fcntl.LOCK_UN)
        archivo.close()

# ---------------- proyectos: un prompt, todas las IA ----------------
ESQUEMA_PLAN = """Devuelve SOLO un JSON valido, sin markdown ni texto alrededor, con esta forma:
{"nombre":"nombre-corto-en-kebab-case",
 "resumen":"una frase de que se va a construir",
 "tareas":[
   {"id":"t1","titulo":"...","tipo":"code|research|writing|edicion|imagen|review",
    "depende":[],"instruccion":"instruccion concreta y autocontenida",
    "archivos":["ruta/que/crea.py"]}
 ]}

Reglas del plan (importantes, el trabajo se reparte entre varias IA en paralelo):
- Entre 4 y 7 tareas.
- MAXIMO PARALELISMO. Varias IA distintas trabajan a la vez, asi que la mayoria
  de las tareas deben tener "depende":[] o depender solo de la primera.
  Una cadena lineal (t2 depende de t1, t3 de t2, t4 de t3...) es un plan MALO
  porque deja a tres IA sin hacer nada. Evitala.
- Para lograrlo, define primero UNA tarea de cimientos (contratos, modelos de
  datos, estructura de archivos) y que el resto dependa solo de ella y se
  reparta modulos que NO se pisan entre si.
- Cada tarea declara en "archivos" que ficheros escribe. Dos tareas de la misma
  ola nunca pueden escribir el mismo fichero.
- Usa tipos variados y realistas, no marques todo como "code": la documentacion
  es "writing", verificar es "review", buscar informacion es "research",
  cualquier grafico o logo es "imagen".
- Cada 'instruccion' dice exactamente que crear, con nombres de archivo.
- Todo vive en UNA sola carpeta; nada de estructuras paralelas.
- No incluyas instalar dependencias del sistema ni desplegar."""


def _reparar_plan(plan, n_cuentas):
    """Informa sobre un plan secuencial sin alterar dependencias semanticas."""
    tareas = plan.get("tareas") or []
    if len(tareas) < 3:
        return plan, None
    olas = _ordenar_por_olas(tareas)
    if max((len(o) for o in olas), default=0) > 1:
        return plan, None                     # ya tiene paralelo
    return plan, ("el plan es secuencial; se respetan sus dependencias para no "
                  "ejecutar pruebas, migraciones o integracion antes de tiempo")


def _mejor_para(tarea, preferir=None):
    r = ranking(tarea, preferir=preferir)
    return (r[0]["pid"], r[0]["p"]) if r else (None, None)


def _json_de(texto):
    """Extrae el primer objeto JSON de una respuesta, tolerando ```json."""
    if not texto:
        return None
    t = texto.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        return None


def validar_plan(plan):
    """Valida el contrato del plan antes de crear hilos o tocar archivos."""
    if not isinstance(plan, dict):
        return None, "el plan no es un objeto JSON"
    tareas = plan.get("tareas")
    if not isinstance(tareas, list) or not tareas:
        return None, "'tareas' debe ser una lista no vacia"
    normalizadas = []
    ids = set()
    for indice, original in enumerate(tareas, 1):
        if not isinstance(original, dict):
            return None, f"tarea {indice}: debe ser un objeto"
        t = dict(original)
        for campo in ("id", "titulo", "instruccion"):
            if not isinstance(t.get(campo), str) or not t[campo].strip():
                return None, f"tarea {indice}: '{campo}' debe ser texto no vacio"
            t[campo] = t[campo].strip()
        if t["id"] in ids:
            return None, f"id de tarea duplicado: {t['id']}"
        ids.add(t["id"])
        depende = t.get("depende", [])
        if not isinstance(depende, list) or not all(
                isinstance(x, str) and x.strip() for x in depende):
            return None, f"tarea {t['id']}: 'depende' debe ser una lista de ids"
        t["depende"] = [x.strip() for x in depende]
        archivos = t.get("archivos", [])
        if not isinstance(archivos, list) or not all(isinstance(x, str) for x in archivos):
            return None, f"tarea {t['id']}: 'archivos' debe ser una lista de rutas"
        rutas = []
        for ruta in archivos:
            limpia = os.path.normpath(ruta.strip()).replace("\\", "/")
            if (not ruta.strip() or os.path.isabs(ruta) or limpia == ".."
                    or limpia.startswith("../")):
                return None, f"tarea {t['id']}: ruta fuera del proyecto: {ruta!r}"
            rutas.append(limpia)
        t["archivos"] = rutas
        tipo = t.get("tipo", "code")
        if tipo not in TAREAS:
            return None, f"tarea {t['id']}: tipo desconocido '{tipo}'"
        t["tipo"] = tipo
        if tipo not in {"reasoning", "research"} and not rutas:
            return None, (f"tarea {t['id']}: una tarea escritora debe declarar "
                          "al menos una ruta en 'archivos'")
        normalizadas.append(t)
    for t in normalizadas:
        desconocidas = [x for x in t["depende"] if x not in ids]
        if desconocidas:
            return None, (f"tarea {t['id']}: dependencias desconocidas: "
                          + ", ".join(desconocidas))
        if t["id"] in t["depende"]:
            return None, f"tarea {t['id']}: no puede depender de si misma"

    # Detectar ciclos antes del ejecutor; de otro modo terminarian como un
    # conjunto generico de bloqueos y ocultarian el defecto del planificador.
    deps = {t["id"]: set(t["depende"]) for t in normalizadas}
    pendientes, hechas = dict(deps), set()
    while pendientes:
        listas = [pid for pid, ds in pendientes.items() if ds <= hechas]
        if not listas:
            return None, "el plan contiene un ciclo de dependencias"
        for pid in listas:
            hechas.add(pid)
            pendientes.pop(pid)

    # Dos tareas que pueden correr a la vez nunca deben declarar el mismo
    # archivo. Si existe una dependencia transitiva entre ellas, el orden las
    # hace seguras; de lo contrario el plan se rechaza antes de crear hilos.
    transitivas = {}
    for tid in deps:
        vistas, pila = set(), list(deps[tid])
        while pila:
            dep = pila.pop()
            if dep in vistas:
                continue
            vistas.add(dep)
            pila.extend(deps.get(dep, ()))
        transitivas[tid] = vistas
    for indice, primera in enumerate(normalizadas):
        for segunda in normalizadas[indice + 1:]:
            solape = sorted(set(primera["archivos"]) & set(segunda["archivos"]))
            ordenadas = (
                primera["id"] in transitivas[segunda["id"]]
                or segunda["id"] in transitivas[primera["id"]]
            )
            if solape and not ordenadas:
                return None, (
                    f"tareas {primera['id']} y {segunda['id']} pueden correr en "
                    f"paralelo y comparten archivos: {', '.join(solape)}"
                )

    limpio = dict(plan)
    limpio["tareas"] = normalizadas
    return limpio, None


def planificar(descripcion, carpeta, timeout=300, preferir=None):
    """Fase 0: la cuenta mas fuerte en 'agentic' propone el plan."""
    pid_inicial, perfil_inicial = _mejor_para("agentic", preferir)
    if not pid_inicial:
        return None, "no hay ninguna cuenta disponible para planificar"
    candidatos = [{"pid": pid_inicial, "p": perfil_inicial}]
    candidatos += [
        x for x in ranking("agentic", preferir=preferir)
        if x["pid"] != pid_inicial
    ]
    prompt = (f"Eres el arquitecto de un proyecto que se construira en la carpeta "
              f"{carpeta}. El encargo es:\n\n{descripcion}\n\n{ESQUEMA_PLAN}")
    errores = []
    tokens = 0
    for indice, candidato in enumerate(candidatos):
        pid, p = candidato["pid"], candidato["p"]
        intento = prompt
        if errores:
            intento += (
                "\n\nRELEVO DE PLANIFICACION: otra cuenta no pudo producir un plan "
                "valido. Genera tu propio JSON a partir del encargo original; no "
                "edites archivos todavia. Fallos anteriores: " + " | ".join(errores[-3:])
            )
        r = correr(pid, p, intento, "agentic", timeout, carpeta=carpeta,
                   solo_lectura=True)
        tokens += r.get("tokens", 0)
        plan = _json_de(r.get("texto")) if r.get("rc") == 0 else None
        if not plan:
            errores.append(f"{pid}: {(r.get('texto') or 'sin plan')[:180]}")
            continue
        plan, error_plan = validar_plan(plan)
        if error_plan:
            errores.append(f"{pid}: {error_plan}")
            continue
        plan["_planificador"] = pid
        plan["_tokens_plan"] = tokens
        plan["_relevos_plan"] = indice
        plan, aviso = _reparar_plan(plan, len(disponibles()))
        plan["_aviso"] = aviso
        return plan, None
    return None, "ninguna cuenta produjo un plan valido: " + " | ".join(errores)


def _ordenar_por_olas(tareas):
    """Agrupa las tareas en olas: cada ola puede correr en paralelo."""
    pend = {t["id"]: t for t in tareas}
    hechas, olas = set(), []
    while pend:
        ola = [t for t in pend.values()
               if all(d in hechas for d in (t.get("depende") or []))]
        if not ola:                       # dependencia rota o ciclo: corre el resto
            ola = list(pend.values())
        olas.append(ola)
        for t in ola:
            hechas.add(t["id"]); pend.pop(t["id"], None)
    return olas


def deliberar(plan, encargo, timeout=240, preferir=None):
    """Las IA opinan sobre quien debe hacer que, antes de gastar en construir.

    Si coinciden en que conviene hacerlo con una sola cuenta, se hace asi.
    """
    disp = disponibles()
    votantes_disp = disponibles(tarea="reasoning")
    if len(votantes_disp) < 2:
        return None, "una sola cuenta disponible; no hay nada que deliberar"
    resumen_plan = "\n".join(f"- {t['id']} [{t.get('tipo','code')}] {t['titulo']}"
                             for t in plan["tareas"])
    fichas = []
    for pid, p in disp.items():
        w = p.get("weights") or {}
        fuerte = sorted(w.items(), key=lambda x: -x[1])[:3]
        try:
            q = cuota(pid, p)
            resto = (f"{100 - q['usado_pct']:.0f}% de cuota libre"
                     if q.get("usado_pct") is not None else "cuota sin medir")
        except Exception:
            resto = "cuota sin medir"
        fichas.append(f"- {pid} ({p.get('provider')}): fuerte en "
                      f"{', '.join(k for k, _ in fuerte)}; {resto}")
    prompt = DELIBERAR.format(encargo=encargo[:600], plan=resumen_plan,
                              cuentas="\n".join(fichas))
    votantes = sorted(
        votantes_disp.items(), key=lambda kv: kv[0] != preferir
    )[:3]
    with ThreadPoolExecutor(max_workers=len(votantes)) as ex:
        votos = list(ex.map(lambda kv: (kv[0], correr(kv[0], kv[1], prompt,
                                                      "reasoning", timeout,
                                                      solo_lectura=True)),
                            votantes))
    props, solos = [], 0
    for pid, r in votos:
        j = _json_de(r["texto"])
        if not j:
            continue
        if j.get("una_sola"):
            solos += 1
        if isinstance(j.get("asignacion"), dict):
            props.append(j["asignacion"])
    if not props:
        return None, "nadie devolvio una asignacion valida; sigo con el router"
    if solos > len(props) / 2:
        # mayoria dice que es mejor una sola cuenta: la mas capaz con cuota
        rk = ranking("agentic", preferir=preferir)
        elegida = rk[0]["pid"] if rk else None
        return ({t["id"]: elegida for t in plan["tareas"]},
                f"{solos} de {len(props)} coinciden en hacerlo con una sola cuenta ({elegida})")
    # consenso por mayoria tarea a tarea
    final, validas = {}, set(disp)
    for t in plan["tareas"]:
        tipo = t.get("tipo", "code")
        if tipo not in TAREAS:
            tipo = "code"
        conteo = {}
        for a in props:
            v = a.get(t["id"])
            if v in validas and admite_tarea(disp[v], tipo):
                conteo[v] = conteo.get(v, 0) + 1
        if conteo:
            final[t["id"]] = max(conteo.items(), key=lambda x: x[1])[0]
    return (final or None), (f"consenso de {len(props)} modelos sobre "
                             f"{len(final)} tareas" if final else "sin consenso")


def ejecutar_proyecto(plan, carpeta, timeout=600, callback=None,
                      asignacion=None, contexto_terminal="", preferir=None):
    """Ejecucion progresiva: cada tarea arranca en cuanto sus dependencias
    terminan con exito. Un fallo nunca libera trabajo dependiente."""
    carpeta = preparar_ruta_trabajo(carpeta, crear=True)
    if not carpeta:
        raise ValueError("carpeta de proyecto fuera de las areas autorizadas")
    pz = Pizarra(carpeta, plan, contexto_terminal)
    pendientes = {t["id"]: t for t in plan["tareas"]}
    ids_plan = set(pendientes)
    hechas, fallidas, resultados = set(), set(), []
    ocupadas = set()
    futuros = {}
    intentos_por_tarea = {t["id"]: [] for t in plan["tareas"]}
    relevos_por_tarea = {t["id"]: [] for t in plan["tareas"]}

    def preparar(t):
        tipo = t.get("tipo", "code")
        if tipo not in TAREAS:
            tipo = "code"
        # Un escritor tiene acceso total al repo. Se ejecuta en exclusiva aun
        # cuando declare archivos distintos, porque herramientas auxiliares y
        # formatters pueden tocar rutas que el planificador no anticipo. Las
        # tareas puramente lectoras si pueden compartir una ola.
        lectores = {"reasoning", "research"}
        hay_escritor = any(
            (meta[0].get("tipo") if meta[0].get("tipo") in TAREAS else "code")
            not in lectores for meta in futuros.values()
        )
        if futuros and (tipo not in lectores or hay_escritor):
            return None, None, tipo, "ocupadas"
        pid = (asignacion or {}).get(t["id"])
        p = cfg().get("profiles", {}).get(pid) if pid else None
        usadas = set(intentos_por_tarea[t["id"]])
        asignada_valida = (pid and p and admite_tarea(p, tipo)
                           and autenticado(pid, p) and not bloqueado(pid)
                           and pid not in usadas)
        if asignada_valida:
            if pid in ocupadas:
                return None, None, tipo, "ocupadas"
            return pid, p, tipo, None
        if not asignada_valida:
            r = ranking(tipo, preferir=preferir)
            nuevas = [x for x in r if x["pid"] not in usadas]
            libre = next((x for x in nuevas if x["pid"] not in ocupadas), None)
            if not libre:
                return None, None, tipo, (
                    "ocupadas" if nuevas else "sin otra cuenta disponible para el relevo"
                )
            pid, p = libre["pid"], libre["p"]
        return pid, p, tipo, None

    def registrar_fallo(t, motivo, rc=125, pid="sin-cuenta", tipo=None):
        tipo = tipo or t.get("tipo", "code")
        texto = f"[ERROR rc={rc}] {motivo}"
        r = {"id": t["id"], "titulo": t["titulo"], "perfil": pid,
             "tipo": tipo, "rc": rc, "tokens": 0, "seg": 0.0,
             "texto": texto, "run_id": None, "estado": "bloqueado"}
        pendientes.pop(t["id"], None)
        fallidas.add(t["id"])
        pz.fallar(t, pid, texto)
        resultados.append(r)
        if callback:
            callback("termina", r)

    def instruccion(t, pid):
        estado = pz.foto(excluir_id=t["id"])
        arch = pz.archivos_reales()
        relevos = relevos_por_tarea[t["id"]]
        entrega = ""
        if relevos:
            detalle = "\n".join(
                f"- {x['perfil']}: rc={x['rc']}, {x['seg']}s, "
                f"sesion={x.get('session_id') or 'n/a'}; {x['texto'][:180]}"
                for x in relevos[-6:]
            )
            entrega = (
                "\nRELEVO CONTROLADO: los procesos anteriores ya terminaron. "
                "No empieces de cero: inspecciona `git status`, `git diff`, los "
                "archivos reales y las pruebas antes de continuar. Conserva lo "
                f"correcto y repara lo incompleto.\n{detalle}\n"
            )
        return (
            f"Proyecto: {plan.get('nombre','proyecto')} — {plan.get('resumen','')}\n"
            f"Carpeta: {carpeta}\n"
            + (f"Archivos que ya existen ahi: {', '.join(arch)}\n" if arch else "")
            + (f"\n{estado}\n" if estado else "")
            + f"\nTU TAREA: {t['titulo']}\n{t['instruccion']}\n"
            + (f"Archivos que te tocan: {', '.join(t['archivos'])}\n"
               if t.get("archivos") else "")
            + f"\nReglas: escribe dentro de {carpeta}. Integra con lo ya hecho en vez "
              f"de duplicarlo. No toques lo que otra IA tiene en curso. "
              f"No ejecutes git commit ni git push: Orquesta publica al final "
              f"solo despues de verificar y escanear secretos. "
              f"Calidad de produccion, no un esqueleto. "
              f"Al terminar responde en UNA linea: que archivos creaste o cambiaste."
            + entrega)

    with ThreadPoolExecutor(max_workers=max(2, len(disponibles()))) as ex:
        while pendientes or futuros:
            # Propagar fallos o dependencias inexistentes antes de lanzar nada.
            for t in list(pendientes.values()):
                deps = set(t.get("depende") or [])
                rotas = sorted((deps - ids_plan) | (deps & fallidas))
                if rotas:
                    registrar_fallo(
                        t, "dependencia fallida o inexistente: " + ", ".join(rotas)
                    )

            listas = [t for t in pendientes.values()
                      if all(d in hechas for d in (t.get("depende") or []))]
            lanzada = False
            for t in listas:
                pid, p, tipo, motivo = preparar(t)
                if not pid:
                    # Si solo estan ocupadas, se reevalua al acabar un futuro.
                    if motivo == "ocupadas" and futuros:
                        continue
                    registrar_fallo(t, motivo or "sin cuenta disponible", 127,
                                    tipo=tipo)
                    continue
                pendientes.pop(t["id"], None)
                ocupadas.add(pid)
                intentos_por_tarea[t["id"]].append(pid)
                lanzada = True
                pz.empezar(t, pid)
                if callback:
                    callback("empieza", {"titulo": t["titulo"], "perfil": pid, "tipo": tipo})
                fut = ex.submit(correr, pid, p, instruccion(t, pid), tipo, timeout, carpeta)
                futuros[fut] = (t, pid)
            if not futuros:
                # No hay tarea ejecutable: el resto forma un ciclo o depende de
                # algo que nunca podra completarse. Antes se ejecutaba a la fuerza.
                for t in list(pendientes.values()):
                    registrar_fallo(t, "ciclo o dependencias no satisfechas")
                break
            # integrar en cuanto CUALQUIERA termine
            done, _ = wait(list(futuros), return_when=FIRST_COMPLETED)
            for fut in done:
                t, pid = futuros.pop(fut)
                try:
                    r = fut.result()
                except Exception as e:
                    r = {"texto": f"[ERROR] {e}", "tokens": 0, "seg": 0, "rc": 1,
                         "run_id": f"{pid}-error"}
                ocupadas.discard(pid)
                if r.get("rc") == 0:
                    hechas.add(t["id"])
                    pz.terminar(t, pid, r.get("texto"))
                else:
                    tipo = t.get("tipo") if t.get("tipo") in TAREAS else "code"
                    disponibles_relevo = [
                        x for x in ranking(tipo, preferir=preferir)
                        if x["pid"] not in set(intentos_por_tarea[t["id"]])
                    ]
                    if disponibles_relevo:
                        pendientes[t["id"]] = t
                        pz.relevar(t, pid, r.get("texto"))
                    else:
                        fallidas.add(t["id"])
                        pz.fallar(t, pid, r.get("texto"))
                res = {"id": t["id"], "titulo": t["titulo"], "perfil": pid,
                       "tipo": t.get("tipo"), "rc": r.get("rc", 1),
                       "tokens": r.get("tokens", 0), "seg": r.get("seg", 0),
                       "texto": (r.get("texto") or "")[:400],
                       "run_id": r.get("run_id"),
                       "session_id": r.get("session_id")}
                if r.get("rc") != 0 and disponibles_relevo:
                    res["estado"] = "relevado"
                    res["relevado"] = True
                    relevos_por_tarea[t["id"]].append(res)
                resultados.append(res)
                if callback:
                    callback("termina", res)
    return resultados


def integrar_proyecto(plan, carpeta, resultados, timeout=600, preferir=None):
    """Fase final: alguien revisa la carpeta entera y la deja conectada."""
    pid, p = _mejor_para("review", preferir)
    if not pid:
        return None
    lineas = []
    presupuesto = 14000
    for r in resultados[-30:]:
        hallazgos = r.get("hallazgos")
        if isinstance(hallazgos, list) and hallazgos:
            detalle = "HALLAZGOS: " + " | ".join(
                str(x).replace("\n", " ")[:700] for x in hallazgos[:8]
            )
        else:
            detalle = (r.get("texto") or "").replace("\n", " ")[:500]
        linea = f"- [{r.get('perfil', '?')}] {r.get('titulo', 'fase')}: {detalle}"
        if sum(len(x) + 1 for x in lineas) + len(linea) > presupuesto:
            break
        lineas.append(linea)
    hecho = "\n".join(lineas)
    prompt = (
        f"Revisa la carpeta {carpeta} del proyecto '{plan.get('nombre')}'.\n"
        f"Lo que hizo cada IA:\n{hecho}\n\n"
        f"Tu trabajo: recorre los archivos reales de la carpeta y dejala COHERENTE. "
        f"Arregla importaciones o rutas que no cuadren entre piezas hechas por "
        f"distintos autores, elimina duplicados y archivos sueltos que no encajen, "
        f"y escribe o corrige el README.md con que es, como se instala y como se usa. "
        f"No reescribas lo que ya funciona. Responde en 5 lineas: que arreglaste.")
    r = correr(pid, p, prompt, "review", timeout, carpeta=carpeta)
    if r.get("rc") == 0:
        return r
    usados = {pid}
    for candidato in ranking("review", preferir=preferir):
        if candidato["pid"] in usados:
            continue
        usados.add(candidato["pid"])
        relevo = (
            prompt + "\n\nRELEVO CONTROLADO: el integrador anterior termino con "
            f"rc={r.get('rc')} y ya no esta escribiendo. Audita el estado real y "
            "continua la integracion sin deshacer cambios correctos."
        )
        r = correr(candidato["pid"], candidato["p"], relevo, "review",
                   timeout, carpeta=carpeta)
        if r.get("rc") == 0:
            return r
    return r


def _argv_verificacion(comando):
    """Convierte una comprobacion propuesta por una IA en argv permitido.

    Nunca se usa un shell. La lista es deliberadamente estrecha: permite
    pruebas, linters, compilacion y consultas Git, pero no gestores de paquetes,
    red, redirecciones ni comandos arbitrarios disfrazados de verificacion.
    """
    if not isinstance(comando, str) or not comando.strip() or len(comando) > 600:
        return None
    if "\n" in comando or "\r" in comando:
        return None
    try:
        argv = shlex.split(comando)
    except ValueError:
        return None
    if not argv or len(argv) > 48 or "/" in argv[0] or "\\" in argv[0]:
        return None
    base = os.path.basename(argv[0])
    args = argv[1:]

    def relativos(valores):
        for valor in valores:
            candidatos = [valor]
            if "=" in valor:
                candidatos.append(valor.split("=", 1)[1])
            for candidato in candidatos:
                partes = candidato.replace("\\", "/").split("/")
                if (candidato.startswith((os.sep, "\\")) or ".." in partes):
                    return False
        return True

    def pytest_seguro(valores):
        prohibidas = ("--basetemp", "--cache-dir", "--rootdir", "--confcutdir")
        return relativos(valores) and not any(
            arg == p or arg.startswith(p + "=") for arg in valores for p in prohibidas
        )

    def ruff_seguro(valores):
        return relativos(valores) and bool(valores) and (
            (valores[0] == "check" and "--fix" not in valores
             and "--unsafe-fixes" not in valores)
            or (valores[0] == "format" and "--check" in valores)
        )

    if base == "git":
        prefijo = ["/usr/bin/git", *_GIT_VERIFICACION]
        if args in (["diff", "--check"], ["diff", "--cached", "--check"],
                    ["diff", "--check", "--cached"]):
            cached = "--cached" in args
            return prefijo + ["-c", "diff.external=", "diff", "--no-ext-diff",
                              "--no-textconv"] + (["--cached"] if cached else []) + ["--check"]
        if (args[:1] == ["status"] and all(
                x in {"--porcelain", "--short", "--branch", "-sb"}
                for x in args[1:])):
            return prefijo + args
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return prefijo + args
        if args[:1] == ["ls-files"] and all(
                x in {"--cached", "--modified", "--deleted", "--others",
                      "--exclude-standard", "-c", "-m", "-d", "-o"}
                for x in args[1:]):
            return prefijo + args
        return None
    permitido = False
    if base in {"python", "python3"}:
        if len(args) < 2 or args[0] != "-m":
            return None
        modulo, modulo_args = args[1], args[2:]
        if modulo == "unittest":
            permitido = relativos(modulo_args)
        elif modulo == "pytest":
            permitido = pytest_seguro(modulo_args)
        elif modulo == "ruff":
            permitido = ruff_seguro(modulo_args)
        elif modulo == "mypy":
            permitido = relativos(modulo_args) and not any(
                x == "--config-file" or x.startswith("--config-file=")
                or x == "--cache-dir" or x.startswith("--cache-dir=")
                for x in modulo_args
            )
    elif base in {"pytest", "py.test", "mypy"}:
        permitido = pytest_seguro(args) if base != "mypy" else relativos(args)
    elif base == "ruff":
        permitido = ruff_seguro(args)
    elif base == "eslint":
        permitido = relativos(args) and "--fix" not in args and not any(
            x == "--output-file" or x.startswith("--output-file=")
            or x in {"--rulesdir", "--resolve-plugins-relative-to"}
            or x.startswith("--rulesdir=")
            or x.startswith("--resolve-plugins-relative-to=")
            for x in args
        )
    elif base == "node":
        permitido = bool(args) and args[0] == "--check" and relativos(args[1:])
    elif base in {"bash", "sh"}:
        permitido = bool(args) and args[0] == "-n" and relativos(args[1:])
    elif base in {"npm", "pnpm", "yarn"}:
        permitido = relativos(args) and (args == ["test"] or (
            len(args) >= 2 and args[0] == "run"
            and args[1] in {"test", "lint", "check", "build", "typecheck"}
            and all(not x.startswith("--script-shell") for x in args[2:])
        ))
    elif base == "cargo":
        permitido = relativos(args) and bool(args) and (
            args[0] in {"test", "check", "clippy"}
            or (args[0] == "fmt" and "--check" in args)
        ) and not any(
            x in {"--config", "--manifest-path", "--target-dir"}
            or x.startswith(("--config=", "--manifest-path=", "--target-dir="))
            for x in args
        )
    elif base == "go":
        permitido = relativos(args) and bool(args) and args[0] in {"test", "vet"} \
            and not any(x == "-exec" or x.startswith("-exec=") for x in args)
    elif base == "make":
        permitido = bool(args) and all(
            not x.startswith("-") and x in {"test", "check", "lint", "build"}
            for x in args
        )
    elif base == "tsc":
        permitido = "--noEmit" in args and relativos(args)
    elif base == "systemd-analyze":
        permitido = relativos(args) and args[:1] == ["verify"] and len(args) >= 2 \
            and all(not x.startswith("-") for x in args[1:])
    return argv if permitido else None


def _operandos_verificacion_seguros(argv, carpeta, cwd_fd=None):
    """Revalida operandos que parecen rutas dentro de la capacidad del repo."""
    if not argv:
        return False
    if cwd_fd is None:
        try:
            with _capacidad_trabajo(carpeta) as (_, fd):
                return _operandos_verificacion_seguros(argv, ".", cwd_fd=fd)
        except OSError:
            return False
    valores = list(argv[1:])
    # El argv Git ya fue sustituido por una plantilla fija sin rutas de entrada.
    if os.path.basename(argv[0]) == "git":
        return True
    ignorar = {
        "discover", "test", "check", "clippy", "fmt", "vet", "run",
        "lint", "build", "typecheck", "format", "verify", "unittest",
        "pytest", "ruff", "mypy",
    }

    def existe(candidato):
        try:
            os.stat(candidato, dir_fd=cwd_fd, follow_symlinks=False)
            return True
        except OSError:
            return False

    def relativo_seguro(candidato):
        partes = candidato.replace("\\", "/").split("/")
        if (os.path.isabs(candidato) or not partes
                or any(parte in ("", "..") for parte in partes)):
            return False
        partes = [parte for parte in partes if parte != "."]
        fd = os.dup(cwd_fd)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            for indice, parte in enumerate(partes):
                try:
                    estado = os.stat(parte, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    return indice == len(partes) - 1
                if stat.S_ISLNK(estado.st_mode):
                    return False
                if indice < len(partes) - 1:
                    if not stat.S_ISDIR(estado.st_mode):
                        return False
                    siguiente = os.open(parte, flags, dir_fd=fd)
                    os.close(fd)
                    fd = siguiente
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    for valor in valores:
        candidatos = []
        if "=" in valor:
            candidatos.append(valor.split("=", 1)[1])
        if not valor.startswith("-"):
            candidatos.append(valor)
        for candidato in candidatos:
            candidato = candidato.split("::", 1)[0]
            if not candidato or candidato in ignorar:
                continue
            if candidato == "./...":
                candidato = "."
            parece_ruta = (
                candidato in {".", "tests", "src"}
                or "/" in candidato or "\\" in candidato
                or existe(candidato)
                or bool(re.search(r"\.(?:py|js|jsx|ts|tsx|service|toml|json|yaml|yml)$",
                                  candidato, re.I))
            )
            if not parece_ruta:
                continue
            if (os.path.isabs(candidato)
                    or ".." in candidato.replace("\\", "/").split("/")):
                return False
            if not relativo_seguro(candidato):
                return False
    return True


def ejecutar_comando_verificacion(comando, carpeta, timeout=600, cwd_fd=None):
    """Ejecuta una comprobacion permitida y devuelve evidencia propia."""
    argv = _argv_verificacion(comando)
    if argv is None:
        return {"comando": str(comando)[:160], "rc": 126,
                "resultado": "comando no permitido por la politica de verificacion"}
    if cwd_fd is not None:
        try:
            return _ejecutar_comando_verificacion_adquirido(
                comando, argv, timeout, cwd_fd
            )
        except OSError:
            return {"comando": str(comando)[:160], "rc": 126,
                    "resultado": "capacidad de verificacion invalida"}
    try:
        with _capacidad_trabajo(carpeta) as (_, cwd_fd):
            return _ejecutar_comando_verificacion_adquirido(
                comando, argv, timeout, cwd_fd
            )
    except OSError:
        return {"comando": str(comando)[:160], "rc": 126,
                "resultado": "carpeta de verificacion no autorizada"}


def _ejecutar_comando_verificacion_adquirido(comando, argv, timeout, cwd_fd):
    """Valida y ejecuta sobre la misma capacidad cwd ya adquirida."""
    estado_cwd = os.fstat(cwd_fd)
    identidad_cwd = [int(estado_cwd.st_dev), int(estado_cwd.st_ino)]
    try:
        with _capacidad_raiz_git(cwd_fd) as (_, repo_fd, _prefijo):
            estado_repo = os.fstat(repo_fd)
            identidad_repo = [int(estado_repo.st_dev), int(estado_repo.st_ino)]
    except OSError:
        identidad_repo = None
    if not _operandos_verificacion_seguros(argv, ".", cwd_fd=cwd_fd):
        return {"comando": str(comando)[:160], "rc": 126,
                "resultado": "ruta de verificacion fuera de la carpeta autorizada"}
    if os.path.basename(argv[0]) == "git":
        config_ejecutable = _config_git_ejecutable(".", cwd_fd=cwd_fd)
        if config_ejecutable is not False:
            return {"comando": str(comando)[:160], "rc": 126,
                    "resultado": ("configuracion Git ejecutable no permitida"
                                  if config_ejecutable else
                                  "no pude auditar la configuracion Git")}
    env = {
        "HOME": HOME_USUARIO,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LC_ALL": os.environ.get("LC_ALL") or "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_SHALLOW_FILE": "/dev/null",
        "GIT_GRAFT_FILE": "/dev/null",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    ejecutable = (argv[0] if os.path.isabs(argv[0])
                  else shutil.which(argv[0], path=env["PATH"]))
    if not ejecutable:
        return {"comando": comando, "rc": 127,
                "resultado": "binario de verificacion no disponible"}
    argv[0] = ejecutable
    limite = max(5, min(900, int(timeout or 600)))
    # La verificación no recibe acceso al bus del user manager. Un scope de
    # systemd necesitaría DBUS/XDG del proceso padre y ampliaría la capacidad del
    # código bajo prueba; un grupo POSIX dedicado conserva el aislamiento de
    # terminación sin contaminar el entorno mínimo del objetivo.
    out, err, rc = _ejecutar_aislado_acotado(
        argv, env, ".", limite,
        limite_salida=64 * 1024, limite_error=64 * 1024,
        usar_scope=False, cwd_fd=cwd_fd,
    )
    salida = ((out or "") + ("\n" if out and err else "") + (err or "")).strip()
    return {"comando": " ".join(shlex.quote(x) for x in argv), "rc": int(rc),
            "resultado": salida[-1600:] or ("sin salida" if rc == 0 else "fallo sin salida"),
            "cwd_identidad": identidad_cwd,
            "repo_identidad": identidad_repo}


def verificar_proyecto(plan, carpeta, resultados, timeout=600, preferir=None,
                       mensaje_snapshot=None):
    """Verificacion final independiente, con pruebas reales y salida estructurada."""
    snapshot = None
    if mensaje_snapshot is not None:
        try:
            snapshot = preparar_snapshot_verificacion(carpeta, mensaje_snapshot)
        except OSError as exc:
            return {
                "perfil": "snapshot", "texto": "[ERROR snapshot] no verificable",
                "tokens": 0, "seg": 0.0, "rc": 126,
                "verificacion_ok": False, "comprobaciones": [],
                "hallazgos": [str(exc)[:240]], "snapshot_verificado": None,
            }
    candidatos = ranking("review", preferir=preferir)
    if not candidatos:
        return {"perfil": "sin-cuenta", "texto": "[ERROR] sin verificador disponible",
                "tokens": 0, "seg": 0.0, "rc": 127,
                "verificacion_ok": False, "comprobaciones": [], "hallazgos": []}
    historial = "\n".join(
        f"- [{x.get('perfil', '?')}] {x.get('titulo', 'fase')}: "
        f"{(x.get('texto') or '')[:180]}" for x in resultados[-20:]
    )
    base = (
        f"VERIFICACION FINAL DE SOLO LECTURA del proyecto en {carpeta}.\n"
        f"Encargo: {plan.get('resumen', '')}\nHistorial de trabajo:\n{historial}\n\n"
        "No edites archivos. Inspecciona el repositorio y ejecuta de verdad las "
        "pruebas, linters, compilacion o smoke tests apropiados. Incluye siempre "
        "al menos una comprobacion objetiva (por ejemplo tests o git diff --check). "
        "No declares ok si un requisito sigue incompleto o un comando falla.\n"
        "Devuelve SOLO JSON valido: "
        '{"ok":true,"comprobaciones":[{"comando":"...","rc":0,'
        '"resultado":"resumen"}],"hallazgos":[]}'
    )
    errores = []
    ultimo = None
    for candidato in candidatos:
        prompt = base
        if errores:
            prompt += ("\n\nOtro verificador no entrego evidencia valida: "
                       + " | ".join(errores[-3:]))
        try:
            if snapshot is None:
                ultimo = correr(
                    candidato["pid"], candidato["p"], prompt, "review",
                    timeout, carpeta=carpeta, solo_lectura=True,
                )
            else:
                with materializar_snapshot_verificacion(snapshot) as (
                        snapshot_cwd, snapshot_fd):
                    ultimo = correr(
                        candidato["pid"], candidato["p"], prompt, "review",
                        timeout, carpeta=snapshot_cwd, solo_lectura=True,
                        cwd_fd=snapshot_fd,
                    )
                    if not _checkout_snapshot_limpio(snapshot_fd, snapshot):
                        ultimo = dict(ultimo)
                        ultimo["rc"] = 126
                        ultimo["texto"] = "el revisor altero el checkout inmutable"
        except OSError as exc:
            errores.append(f"snapshot: {str(exc)[:160]}")
            continue
        if ultimo.get("rc") != 0:
            errores.append(f"{candidato['pid']}: rc={ultimo.get('rc')}")
            continue
        dato = _json_de(ultimo.get("texto"))
        comprobaciones = dato.get("comprobaciones") if isinstance(dato, dict) else None
        hallazgos = dato.get("hallazgos") if isinstance(dato, dict) else None
        valido = (
            isinstance(dato, dict)
            and isinstance(dato.get("ok"), bool)
            and isinstance(comprobaciones, list) and bool(comprobaciones)
            and len(comprobaciones) <= 8
            and all(isinstance(x, dict) and isinstance(x.get("comando"), str)
                    for x in comprobaciones)
            and isinstance(hallazgos, list)
        )
        if not valido:
            errores.append(f"{candidato['pid']}: evidencia JSON invalida")
            continue
        if any(_argv_verificacion(x["comando"]) is None for x in comprobaciones):
            errores.append(f"{candidato['pid']}: propuso una comprobacion no permitida")
            continue
        comprobaciones_reales = []
        for comprobacion in comprobaciones:
            if snapshot is None:
                prueba = ejecutar_comando_verificacion(
                    comprobacion["comando"], carpeta, timeout
                )
            else:
                try:
                    with materializar_snapshot_verificacion(snapshot) as (
                            snapshot_cwd, snapshot_fd):
                        prueba = ejecutar_comando_verificacion(
                            comprobacion["comando"], snapshot_cwd, timeout,
                            cwd_fd=snapshot_fd,
                        )
                        if not _checkout_snapshot_limpio(snapshot_fd, snapshot):
                            prueba = dict(prueba)
                            prueba["rc"] = 126
                            prueba["resultado"] = (
                                "la comprobacion altero el checkout inmutable"
                            )
                except OSError as exc:
                    prueba = {
                        "comando": comprobacion["comando"], "rc": 126,
                        "resultado": f"snapshot no disponible: {str(exc)[:160]}",
                    }
            comprobaciones_reales.append(prueba)
        hallazgos_reales = list(hallazgos)
        for prueba in comprobaciones_reales:
            if prueba["rc"] != 0:
                hallazgos_reales.append(
                    f"fallo real rc={prueba['rc']}: {prueba['comando']}"
                )
        identidades = ({tuple(snapshot["cwd_identidad"])} if snapshot else {
            tuple(prueba.get("cwd_identidad", ()))
            for prueba in comprobaciones_reales
        })
        identidad_coherente = (
            len(identidades) == 1
            and len(next(iter(identidades), ())) == 2
            and all(isinstance(x, int) for x in next(iter(identidades), ()))
        )
        if not identidad_coherente:
            hallazgos_reales.append(
                "las comprobaciones no conservaron una identidad cwd unica"
            )
        identidades_repo = ({tuple(snapshot["repo_identidad"])} if snapshot else {
            tuple(prueba.get("repo_identidad") or ())
            for prueba in comprobaciones_reales
        })
        identidad_repo = next(iter(identidades_repo), ())
        identidad_repo_coherente = (
            len(identidades_repo) == 1
            and (not identidad_repo or (
                len(identidad_repo) == 2
                and all(isinstance(x, int) for x in identidad_repo)
            ))
        )
        if not identidad_repo_coherente:
            hallazgos_reales.append(
                "las comprobaciones no conservaron una raiz Git unica"
            )
        ultimo["verificacion_ok"] = bool(
            dato["ok"] and all(x["rc"] == 0 for x in comprobaciones_reales)
            and identidad_coherente and identidad_repo_coherente
            and not hallazgos_reales
        )
        ultimo["cwd_identidad"] = (
            list(next(iter(identidades))) if identidad_coherente else None
        )
        ultimo["repo_identidad"] = (
            list(identidad_repo) if identidad_repo_coherente and identidad_repo
            else None
        )
        ultimo["snapshot_verificado"] = (
            dict(snapshot) if ultimo["verificacion_ok"] and snapshot else None
        )
        ultimo["comprobaciones"] = comprobaciones_reales
        ultimo["hallazgos"] = hallazgos_reales
        return ultimo
    ultimo = ultimo or {"perfil": "sin-cuenta", "tokens": 0, "seg": 0.0}
    ultimo["rc"] = ultimo.get("rc") or 1
    ultimo["texto"] = "[ERROR verificacion] " + " | ".join(errores)
    ultimo["verificacion_ok"] = False
    ultimo["comprobaciones"] = []
    ultimo["hallazgos"] = errores
    return ultimo


def _git(carpeta, *args, timeout=120, cwd_fd=None, indice=None,
         identidad_orquesta=False):
    """Git con configuración ejecutable neutralizada y salida acotada."""
    argv = ["/usr/bin/git", *_GIT_INTERNO, *args]
    env = {
        "HOME": HOME_USUARIO,
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_SHALLOW_FILE": "/dev/null",
        "GIT_GRAFT_FILE": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if indice is not None:
        indice = os.path.abspath(indice)
        padre = os.path.dirname(indice)
        estado_padre = os.stat(padre, follow_symlinks=False)
        if (not stat.S_ISDIR(estado_padre.st_mode)
                or estado_padre.st_uid != os.getuid()
                or estado_padre.st_mode & 0o077):
            raise OSError("indice Git alterno fuera de un directorio privado")
        env["GIT_INDEX_FILE"] = indice
    if identidad_orquesta:
        env.update({
            "GIT_AUTHOR_NAME": "Orquesta IA",
            "GIT_AUTHOR_EMAIL": "orquesta@localhost",
            "GIT_COMMITTER_NAME": "Orquesta IA",
            "GIT_COMMITTER_EMAIL": "orquesta@localhost",
        })
    out, err, rc = _ejecutar_aislado_acotado(
        argv, env, carpeta, timeout,
        limite_salida=8 * 1024 * 1024, limite_error=1024 * 1024,
        usar_scope=False, cwd_fd=cwd_fd,
    )
    return subprocess.CompletedProcess(argv, rc, out, err)


_CONFIG_GIT_EJECUTABLE = (
    r"^(include(if\..*)?\.path|filter\..*\.(clean|smudge|process)|"
    r"diff\..*\.(command|textconv)|merge\..*\.driver|"
    r"core\.(sshcommand|gitproxy|alternaterefscommand|worktree)|"
    r"extensions\.worktreeconfig|credential(\..*)?\.helper|"
    r"gpg(\..*)?\.program|remote\..*\.(uploadpack|receivepack|vcs)|"
    r"url\..*\.(insteadof|pushinsteadof))$"
)


def _config_git_ejecutable(carpeta, cwd_fd=None):
    """Audita extensiones locales que podrían convertir Git en ejecución.

    ``--local`` excluye nuestras opciones ``-c`` y los archivos globales; con
    ``--includes`` también se ven explícitamente los includes declarados por el
    repositorio. ``False`` significa limpio, ``True`` hallazgo y ``None`` que la
    auditoría no pudo completarse.
    """
    r = _git(
        carpeta, "config", "--local", "--includes", "--name-only",
        "--get-regexp", _CONFIG_GIT_EJECUTABLE, timeout=10, cwd_fd=cwd_fd,
    )
    if r is None or r.returncode not in (0, 1):
        return None
    return r.returncode == 0 and bool(r.stdout.strip())


def _oid_git_valido(valor):
    return isinstance(valor, str) and bool(
        re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", valor)
    )


def _rama_git_valida(valor):
    return (isinstance(valor, str)
            and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", valor))
            and ".." not in valor and "//" not in valor and "@{" not in valor
            and not valor.endswith((".", "/", ".lock")))


def _rama_remota_auditable(valor):
    """Equivalente acotado de check-ref-format para inventario remoto."""
    if (not isinstance(valor, str) or not valor or len(valor) > 4096
            or valor.startswith(("/", ".")) or valor.endswith((".", "/"))
            or ".." in valor or "//" in valor or "@{" in valor
            or any(ord(c) < 32 or ord(c) == 127 or c in " ~^:?*[\\"
                   for c in valor)):
        return False
    return all(parte and not parte.startswith(".")
               and not parte.endswith(".lock") for parte in valor.split("/"))


def _git_directo_bytes(repo_fd, modo, oid=None, entrada=None, indice=None,
                       limite_salida=64 * 1024 * 1024):
    """Ejecuta solo primitivas Git sin filtros con argv reconstruido y stdin."""
    if modo == "hash-object":
        argv = ["/usr/bin/git", *_GIT_INTERNO,
                "hash-object", "-w", "--no-filters", "--stdin"]
    elif modo == "update-index":
        argv = ["/usr/bin/git", *_GIT_INTERNO,
                "update-index", "-z", "--index-info"]
    elif modo == "cat-size" and _oid_git_valido(oid):
        argv = ["/usr/bin/git", *_GIT_INTERNO, "cat-file", "-s", oid]
    elif modo == "cat-blob" and _oid_git_valido(oid):
        argv = ["/usr/bin/git", *_GIT_INTERNO, "cat-file", "blob", oid]
    else:
        raise OSError("primitiva Git binaria no permitida")
    env = {
        "HOME": HOME_USUARIO, "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_SHALLOW_FILE": "/dev/null", "GIT_GRAFT_FILE": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if indice is not None:
        env["GIT_INDEX_FILE"] = os.path.abspath(indice)
    try:
        resultado = _SUBPROCESS_RUN_ORIGINAL(
            argv, input=entrada, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=f"/proc/self/fd/{repo_fd}", pass_fds=(repo_fd,), env=env,
            timeout=180, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError("fallo una primitiva Git sin filtros") from exc
    if (len(resultado.stdout or b"") > limite_salida
            or len(resultado.stderr or b"") > 1024 * 1024):
        raise OSError("salida excesiva de primitiva Git")
    return resultado


def _leer_regular_repo_fd(repo_fd, ruta, limite=32 * 1024 * 1024):
    """Lee un path Git regular por openat; ``None`` representa un borrado."""
    if (not isinstance(ruta, str) or not ruta or "\ufffd" in ruta
            or os.path.isabs(ruta)):
        raise OSError("ruta Git no representable")
    partes = ruta.split("/")
    if any(parte in ("", ".", "..") for parte in partes):
        raise OSError("ruta Git insegura")
    flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) \
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    dir_fd = os.dup(repo_fd)
    try:
        for parte in partes[:-1]:
            siguiente = os.open(parte, flags_dir, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = siguiente
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) \
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(partes[-1], flags, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise OSError("symlinks y archivos especiales Git no permitidos") from exc
        try:
            antes = os.fstat(fd)
            if (not stat.S_ISREG(antes.st_mode) or antes.st_nlink != 1
                    or antes.st_size < 0 or antes.st_size > limite):
                raise OSError("archivo Git no regular, enlazado o excesivo")
            bloques, restantes = [], antes.st_size
            while restantes:
                bloque = os.read(fd, min(64 * 1024, restantes))
                if not bloque:
                    raise OSError("archivo Git truncado durante lectura")
                bloques.append(bloque)
                restantes -= len(bloque)
            despues = os.fstat(fd)
            identidad = lambda st: (
                st.st_dev, st.st_ino, st.st_mode, st.st_size,
                st.st_mtime_ns, st.st_ctime_ns,
            )
            if identidad(antes) != identidad(despues):
                raise OSError("archivo Git cambio durante lectura")
            modo = "100755" if antes.st_mode & 0o111 else "100644"
            return b"".join(bloques), modo
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def _tree_publicable_fd(repo_fd, base_oid):
    """Hashea bytes crudos publicables sin ejecutar clean/smudge/process."""
    if base_oid is not None and not _oid_git_valido(base_oid):
        raise OSError("base Git invalida para el indice alterno")
    marcas = _git(".", "ls-files", "-v", "-z", cwd_fd=repo_fd)
    if not marcas or marcas.returncode != 0 or len(marcas.stdout) > 8 * 1024 * 1024:
        raise OSError("no pude auditar flags del indice")
    for registro in marcas.stdout.split("\x00"):
        if not registro:
            continue
        if len(registro) < 3 or registro[1] != " ":
            raise OSError("marca del indice invalida")
        if registro[0] == "S" or registro[0].islower():
            raise OSError("skip-worktree y assume-unchanged no son verificables")
    lista = _git(".", "ls-files", "-co", "--exclude-standard", "-z",
                 cwd_fd=repo_fd)
    if not lista or lista.returncode != 0 or len(lista.stdout) > 8 * 1024 * 1024:
        raise OSError("no pude enumerar el contenido publicable")
    rutas = lista.stdout.split("\x00")
    if rutas and rutas[-1] == "":
        rutas.pop()
    if len(rutas) > 100_000 or len(set(rutas)) != len(rutas):
        raise OSError("lista de paths Git invalida o excesiva")
    entradas = []
    bytes_totales = 0
    for ruta in rutas:
        leido = _leer_regular_repo_fd(repo_fd, ruta)
        if leido is None:
            continue
        contenido, modo = leido
        bytes_totales += len(contenido)
        if bytes_totales > 512 * 1024 * 1024:
            raise OSError("snapshot Git excede el presupuesto total")
        objeto = _git_directo_bytes(
            repo_fd, "hash-object", entrada=contenido, limite_salida=256
        )
        oid = objeto.stdout.decode("ascii", "strict").strip() \
            if objeto.returncode == 0 else ""
        if not _oid_git_valido(oid):
            raise OSError("blob Git verificable invalido")
        entradas.append(
            modo.encode("ascii") + b" " + oid.encode("ascii") + b"\t"
            + os.fsencode(ruta) + b"\x00"
        )
    with tempfile.TemporaryDirectory(prefix="orq-index-", dir=TEMP_ROOT) as privado:
        os.chmod(privado, 0o700)
        indice = os.path.join(privado, "index")
        vacio = _git(".", "read-tree", "--empty", cwd_fd=repo_fd, indice=indice)
        if not vacio or vacio.returncode != 0:
            raise OSError("no pude iniciar el indice verificable")
        if entradas:
            actualizado = _git_directo_bytes(
                repo_fd, "update-index", entrada=b"".join(entradas), indice=indice,
                limite_salida=1024,
            )
            if actualizado.returncode != 0:
                raise OSError("no pude poblar el indice verificable")
        escrito = _git(".", "write-tree", cwd_fd=repo_fd, indice=indice)
        tree_oid = escrito.stdout.strip() if escrito and escrito.returncode == 0 else ""
        if not _oid_git_valido(tree_oid):
            raise OSError("tree Git verificable invalido")
        return tree_oid


def _materializar_tree_fd(repo_fd, destino_fd, tree_oid):
    """Escribe blobs regulares de un tree sin pasar por filtros de checkout."""
    if not _oid_git_valido(tree_oid):
        raise OSError("tree no materializable")
    listado = _git(".", "ls-tree", "-r", "-z", tree_oid, cwd_fd=repo_fd)
    if not listado or listado.returncode != 0 or len(listado.stdout) > 8 * 1024 * 1024:
        raise OSError("manifest de tree invalido o excesivo")
    registros = listado.stdout.split("\x00")
    if registros and registros[-1] == "":
        registros.pop()
    if len(registros) > 100_000:
        raise OSError("tree con demasiadas entradas")
    presupuesto = 512 * 1024 * 1024
    flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) \
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    for registro in registros:
        if "\ufffd" in registro or "\t" not in registro:
            raise OSError("entrada de tree no representable")
        cabecera, ruta = registro.split("\t", 1)
        campos = cabecera.split(" ")
        if (len(campos) != 3 or campos[0] not in {"100644", "100755"}
                or campos[1] != "blob" or not _oid_git_valido(campos[2])):
            raise OSError("modo de tree no reproducible")
        partes = ruta.split("/")
        if (not ruta or os.path.isabs(ruta) or partes[0] == ".git"
                or any(parte in ("", ".", "..") for parte in partes)):
            raise OSError("ruta de tree insegura")
        tamano_r = _git_directo_bytes(
            repo_fd, "cat-size", oid=campos[2], limite_salida=128
        )
        try:
            tamano = int(tamano_r.stdout.strip()) if tamano_r.returncode == 0 else -1
        except ValueError as exc:
            raise OSError("tamano de blob invalido") from exc
        if tamano < 0 or tamano > 32 * 1024 * 1024 or tamano > presupuesto:
            raise OSError("blob de snapshot excesivo")
        blob_r = _git_directo_bytes(
            repo_fd, "cat-blob", oid=campos[2], limite_salida=tamano
        )
        if blob_r.returncode != 0 or len(blob_r.stdout) != tamano:
            raise OSError("blob de snapshot corrupto")
        presupuesto -= tamano
        dir_fd = os.dup(destino_fd)
        try:
            for parte in partes[:-1]:
                try:
                    os.mkdir(parte, 0o700, dir_fd=dir_fd)
                except FileExistsError:
                    pass
                siguiente = os.open(parte, flags_dir, dir_fd=dir_fd)
                os.close(dir_fd)
                dir_fd = siguiente
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL \
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(partes[-1], flags, 0o600, dir_fd=dir_fd)
            try:
                vista = memoryview(blob_r.stdout)
                while vista:
                    escritos = os.write(fd, vista)
                    if escritos <= 0:
                        raise OSError("escritura incompleta de snapshot")
                    vista = vista[escritos:]
                os.fchmod(fd, 0o755 if campos[0] == "100755" else 0o644)
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)


def _escanear_commit_fd(repo_fd, commit_oid):
    if not _oid_git_valido(commit_oid):
        return False
    scanner = os.path.join(CODIGO_ORQUESTA, "tools", "scan-secretos.sh")
    if not os.path.isfile(scanner):
        return False
    argv = ["/usr/bin/bash", scanner, "--commit", commit_oid, "--repo", "."]
    _out, _err, rc = _ejecutar_aislado_acotado(
        argv, {"HOME": HOME_USUARIO, "PATH": "/usr/bin:/bin",
               "LC_ALL": "C.UTF-8"}, ".", 180,
        limite_salida=2 * 1024 * 1024, limite_error=2 * 1024 * 1024,
        usar_scope=False, cwd_fd=repo_fd,
    )
    return rc == 0


def preparar_snapshot_verificacion(carpeta, mensaje):
    """Crea un commit inmutable, no referenciado, del contenido a probar."""
    mensaje = str(mensaje or "chore: snapshot verificado por Orquesta IA")[:200]
    if not mensaje:
        raise OSError("mensaje de snapshot vacio")
    with _capacidad_trabajo(carpeta) as (_, cwd_fd):
        estado_cwd = os.fstat(cwd_fd)
        with _capacidad_raiz_git(cwd_fd) as (raiz, repo_fd, prefijo):
            estado_repo = os.fstat(repo_fd)
            if _config_git_ejecutable(".", cwd_fd=repo_fd) is not False:
                raise OSError("configuracion Git ejecutable no permitida")
            rama_r = _git(".", "symbolic-ref", "--quiet", "--short", "HEAD",
                          cwd_fd=repo_fd)
            rama = rama_r.stdout.strip() if rama_r and rama_r.returncode == 0 else ""
            if not _rama_git_valida(rama):
                raise OSError("rama Git no publicable")
            base_r = _git(".", "rev-parse", "HEAD", cwd_fd=repo_fd)
            base_oid = base_r.stdout.strip() if base_r and base_r.returncode == 0 else ""
            if not _oid_git_valido(base_oid):
                ref_local = _git(
                    ".", "show-ref", "--verify", "--quiet",
                    f"refs/heads/{rama}", cwd_fd=repo_fd,
                )
                if not ref_local or ref_local.returncode != 1:
                    raise OSError("HEAD base invalido")
                base_oid = None
            tree_oid = _tree_publicable_fd(repo_fd, base_oid)
            listado = _git(".", "ls-tree", "-r", tree_oid, cwd_fd=repo_fd)
            if not listado or listado.returncode != 0:
                raise OSError("no pude auditar el tree verificable")
            modos_no_reproducibles = ("120000 ", "160000 ")
            if any(linea.startswith(modos_no_reproducibles)
                   for linea in listado.stdout.splitlines()):
                raise OSError(
                    "symlinks y submodulos no permitidos en snapshot verificable"
                )
            if base_oid is None:
                tree_base = None
            else:
                tree_base_r = _git(
                    ".", "rev-parse", f"{base_oid}^{{tree}}", cwd_fd=repo_fd
                )
                tree_base = (tree_base_r.stdout.strip()
                             if tree_base_r and tree_base_r.returncode == 0 else "")
            creado = base_oid is None or tree_oid != tree_base
            if creado:
                commit_args = ["commit-tree", tree_oid]
                if base_oid is not None:
                    commit_args.extend(["-p", base_oid])
                commit_args.extend(["-m", mensaje])
                commit_r = _git(
                    ".", *commit_args, cwd_fd=repo_fd, identidad_orquesta=True,
                )
                commit_oid = (commit_r.stdout.strip()
                              if commit_r and commit_r.returncode == 0 else "")
            else:
                commit_oid = base_oid
            if not _oid_git_valido(commit_oid):
                raise OSError("commit verificable invalido")
            commit_tree_r = _git(
                ".", "rev-parse", f"{commit_oid}^{{tree}}", cwd_fd=repo_fd
            )
            if (not commit_tree_r or commit_tree_r.returncode != 0
                    or commit_tree_r.stdout.strip() != tree_oid):
                raise OSError("el commit verificable no contiene el tree fijado")
            if _config_git_ejecutable(".", cwd_fd=repo_fd) is not False:
                raise OSError("la configuracion Git cambio durante el snapshot")
            # El objeto queda deliberadamente sin ref hasta superar la revisión.
            testigo_historial = _testigo_historial_git(repo_fd)
            if not _escanear_commit_fd(repo_fd, commit_oid):
                raise OSError("el snapshot no supero el escaneo de secretos")
            if _testigo_historial_git(repo_fd) != testigo_historial:
                raise OSError("la topologia Git cambio durante el snapshot")
            return {
                "version": 1, "base_oid": base_oid, "tree_oid": tree_oid,
                "commit_oid": commit_oid, "rama": rama,
                "cwd_rel": prefijo[:-1] if prefijo else "",
                "cwd_identidad": [estado_cwd.st_dev, estado_cwd.st_ino],
                "repo_identidad": [estado_repo.st_dev, estado_repo.st_ino],
                "raiz": raiz, "creado": creado, "mensaje": mensaje,
            }


def _snapshot_valido(snapshot):
    return (isinstance(snapshot, dict) and snapshot.get("version") == 1
            and (_oid_git_valido(snapshot.get("base_oid"))
                 or (snapshot.get("base_oid") is None
                     and snapshot.get("creado") is True))
            and all(_oid_git_valido(snapshot.get(k))
                    for k in ("tree_oid", "commit_oid"))
            and _rama_git_valida(snapshot.get("rama"))
            and isinstance(snapshot.get("cwd_rel"), str)
            and not os.path.isabs(snapshot["cwd_rel"])
            and ".." not in snapshot["cwd_rel"].split("/")
            and isinstance(snapshot.get("raiz"), str) and bool(snapshot["raiz"])
            and type(snapshot.get("creado")) is bool
            and isinstance(snapshot.get("mensaje"), str)
            and 0 < len(snapshot["mensaje"]) <= 200
            and all(isinstance(snapshot.get(k), list)
                    and len(snapshot[k]) == 2
                    and all(type(x) is int for x in snapshot[k])
                    for k in ("cwd_identidad", "repo_identidad")))


def _checkout_snapshot_limpio(cwd_fd, snapshot):
    try:
        with _capacidad_raiz_git(cwd_fd) as (_, repo_fd, _prefijo):
            head = _git(".", "rev-parse", "HEAD", cwd_fd=repo_fd)
            estado = _git(
                ".", "status", "--porcelain=v2", "-z",
                "--untracked-files=all", cwd_fd=repo_fd,
            )
            return (head and head.returncode == 0
                    and head.stdout.strip() == snapshot["commit_oid"]
                    and estado and estado.returncode == 0 and not estado.stdout)
    except OSError:
        return False


@contextlib.contextmanager
def materializar_snapshot_verificacion(snapshot):
    """Entrega un checkout detached privado y lo retira tras matar procesos."""
    if not _snapshot_valido(snapshot):
        raise OSError("snapshot de verificacion invalido")
    padre = tempfile.mkdtemp(prefix="orq-verify-", dir=TEMP_ROOT)
    try:
        padre_fd = os.open(
            padre,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError:
        # ``mkdtemp`` acaba de crear un directorio privado y vacio. Si ni
        # siquiera podemos adquirirlo, no dejamos ese nombre abandonado.
        try:
            os.rmdir(padre)
        except OSError:
            pass
        raise
    try:
        estado_padre = os.fstat(padre_fd)
        os.fchmod(padre_fd, 0o700)
    except OSError:
        os.close(padre_fd)
        try:
            os.rmdir(padre)
        except OSError:
            pass
        raise
    checkout = os.path.join(padre, "checkout")
    worktree_agregado = False
    retiro_ok = False
    try:
        with _capacidad_trabajo(snapshot["raiz"]) as (_, cwd_raiz_fd):
            with _capacidad_raiz_git(cwd_raiz_fd) as (_, repo_fd, _prefijo):
                estado_repo = os.fstat(repo_fd)
                if [estado_repo.st_dev, estado_repo.st_ino] != snapshot["repo_identidad"]:
                    raise OSError("la raiz del snapshot cambio")
                tree_commit_r = _git(
                    ".", "rev-parse", f"{snapshot['commit_oid']}^{{tree}}",
                    cwd_fd=repo_fd,
                )
                if (not tree_commit_r or tree_commit_r.returncode != 0
                        or tree_commit_r.stdout.strip() != snapshot["tree_oid"]):
                    raise OSError("el objeto snapshot no coincide con su tree")
                agregado = _git(
                    ".", "worktree", "add", "--no-checkout", "--detach",
                    checkout, snapshot["commit_oid"], cwd_fd=repo_fd, timeout=180,
                )
                if not agregado or agregado.returncode != 0:
                    raise OSError("no pude crear el checkout verificable")
                worktree_agregado = True
                try:
                    with _capacidad_trabajo(checkout) as (_, checkout_fd):
                        if _config_git_ejecutable(".", cwd_fd=checkout_fd) is not False:
                            raise OSError("configuracion ejecutable en worktree")
                        indexado = _git(
                            ".", "read-tree", snapshot["tree_oid"],
                            cwd_fd=checkout_fd,
                        )
                        if not indexado or indexado.returncode != 0:
                            raise OSError("no pude fijar el indice del checkout")
                        _materializar_tree_fd(
                            checkout_fd, checkout_fd, snapshot["tree_oid"]
                        )
                        if _config_git_ejecutable(".", cwd_fd=checkout_fd) is not False:
                            raise OSError("la configuracion Git cambio al materializar")
                    cwd_snapshot = (checkout if not snapshot["cwd_rel"] else
                                    os.path.join(checkout, *snapshot["cwd_rel"].split("/")))
                    with _capacidad_trabajo(cwd_snapshot) as (visible, snapshot_cwd_fd):
                        if not _checkout_snapshot_limpio(snapshot_cwd_fd, snapshot):
                            raise OSError("checkout verificable no esta limpio")
                        yield visible, snapshot_cwd_fd
                finally:
                    retirado = _git(
                        ".", "worktree", "remove", "--force", checkout,
                        cwd_fd=repo_fd, timeout=180,
                    )
                    retiro_ok = bool(retirado and retirado.returncode == 0)
                    if not retiro_ok:
                        raise OSError(
                            "no pude retirar el worktree; quedo en cuarentena"
                        )
    finally:
        # Antes de que ``git worktree add`` termine no existe nada que
        # cuarentenizar. Retiramos un checkout vacio que Git haya alcanzado a
        # crear y luego el padre, siempre mediante el descriptor adquirido.
        limpiar_padre = retiro_ok or not worktree_agregado
        if limpiar_padre and not worktree_agregado:
            try:
                os.rmdir("checkout", dir_fd=padre_fd)
            except FileNotFoundError:
                pass
            except OSError:
                limpiar_padre = False
        if limpiar_padre:
            try:
                limpiar_padre = not os.listdir(padre_fd)
            except OSError:
                limpiar_padre = False
        os.close(padre_fd)
        if limpiar_padre:
            try:
                estado_visible = os.stat(padre, follow_symlinks=False)
                if ((estado_visible.st_dev, estado_visible.st_ino)
                        == (estado_padre.st_dev, estado_padre.st_ino)):
                    os.rmdir(padre)
            except OSError:
                pass


def _testigo_historial_git(repo_fd):
    """Rechaza grafts/shallow y atestigua directorios que los alojan."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        git_fd = os.open(".git", flags, dir_fd=repo_fd)
    except NotADirectoryError as exc:
        # En un linked worktree ``.git`` es un archivo que redirige a un
        # directorio común mutable. Seguirlo ampliaría la capacidad adquirida
        # y permitiría que grafts/shallow cambiasen fuera del repo_fd. Hasta
        # modelar ambos gitdirs por descriptor, la publicación falla cerrada.
        raise OSError(
            "los linked worktrees no se admiten para publicacion verificable; "
            "use el checkout principal"
        ) from exc
    try:
        estado_git = os.fstat(git_fd)
        try:
            info_fd = os.open("info", flags, dir_fd=git_fd)
        except FileNotFoundError:
            info_fd = None
        try:
            estado_info = os.fstat(info_fd) if info_fd is not None else None
            for nombre, fd in (("shallow", git_fd), ("grafts", info_fd)):
                if fd is None:
                    continue
                try:
                    os.stat(nombre, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise OSError("historial Git shallow/grafts no verificable")
        finally:
            if info_fd is not None:
                os.close(info_fd)
        def identidad(estado):
            if estado is None:
                return None
            return (estado.st_dev, estado.st_ino, estado.st_mode,
                    estado.st_mtime_ns, estado.st_ctime_ns)

        return identidad(estado_git), identidad(estado_info)
    finally:
        os.close(git_fd)


def _refs_remotas_confiables(repo_fd, remoto_fetch):
    """Obtiene refs de origin en un namespace efimero, no desde refs locales."""
    if not _remoto_git_permitido(remoto_fetch):
        raise OSError("URL de fetch no permitida")
    token = secrets.token_hex(16)
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise OSError("token remoto invalido")
    namespace = f"refs/orq-audit/{token}/"

    def listar_namespace_crudo():
        r = _git(
            ".", "for-each-ref", "--format=%(refname)%09%(objectname)",
            namespace, cwd_fd=repo_fd,
        )
        if not r or r.returncode != 0 or len(r.stdout) > 8 * 1024 * 1024:
            raise OSError("no pude enumerar el namespace remoto")
        registros = []
        for linea in r.stdout.splitlines():
            if not linea:
                continue
            partes = linea.split("\t")
            if len(partes) != 2 or not partes[0].startswith(namespace):
                raise OSError("ref temporal remota invalida")
            if not _oid_git_valido(partes[1]):
                raise OSError("OID temporal remoto invalido")
            registros.append((partes[0], partes[1]))
        return registros

    def validar_namespace(registros):
        mapa = {}
        for ref_completa, oid in registros:
            rama = ref_completa[len(namespace):]
            if (not _rama_remota_auditable(rama) or rama in mapa):
                raise OSError("mapa temporal remoto invalido")
            mapa[rama] = oid
        if len(mapa) > MAX_REFS_REMOTAS:
            raise OSError("demasiadas refs remotas")
        return mapa

    def leer_ls_remote():
        remoto = _git(
            ".", "ls-remote", "--heads", "--", remoto_fetch, timeout=180,
            cwd_fd=repo_fd,
        )
        if not remoto or remoto.returncode != 0 or len(remoto.stdout) > 8 * 1024 * 1024:
            raise OSError("no pude contrastar refs remotas")
        mapa = {}
        for linea in remoto.stdout.splitlines():
            partes = linea.split("\t")
            prefijo = "refs/heads/"
            if (len(partes) != 2 or not partes[1].startswith(prefijo)
                    or not _oid_git_valido(partes[0])):
                raise OSError("salida ls-remote invalida")
            rama = partes[1][len(prefijo):]
            if not _rama_remota_auditable(rama) or rama in mapa:
                raise OSError("mapa ls-remote invalido")
            mapa[rama] = partes[0]
        if len(mapa) > MAX_REFS_REMOTAS:
            raise OSError("demasiadas refs remotas")
        return mapa

    if listar_namespace_crudo():
        raise OSError("namespace remoto no estaba vacio")
    mapa_fetch = {}
    error = None
    try:
        mapa_antes = leer_ls_remote()
        refspec = f"+refs/heads/*:{namespace}*"
        fetch = _git(
            ".", "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
            "--", remoto_fetch, refspec,
            timeout=180, cwd_fd=repo_fd,
        )
        if not fetch or fetch.returncode != 0:
            raise OSError("no pude obtener refs explicitas de origin")
        mapa_fetch = validar_namespace(listar_namespace_crudo())
        mapa_despues = leer_ls_remote()
        if mapa_antes != mapa_fetch or mapa_despues != mapa_fetch:
            raise OSError("origin cambio durante la adquisicion de refs")
    except OSError as exc:
        error = exc
    finally:
        try:
            actuales = listar_namespace_crudo()
            for ref_completa, oid in actuales:
                borrado = _git(
                    ".", "update-ref", "-d", ref_completa, oid,
                    cwd_fd=repo_fd,
                )
                if not borrado or borrado.returncode != 0:
                    raise OSError("no pude limpiar una ref remota temporal")
            if listar_namespace_crudo():
                raise OSError("namespace remoto temporal no quedo vacio")
        except OSError as exc:
            error = error or exc
    if error is not None:
        raise error
    return mapa_fetch


def _rev_list_excluyendo_refs(repo_fd, commit_oid, oids_remotos):
    if not _oid_git_valido(commit_oid):
        return None
    unicos = tuple(dict.fromkeys(oids_remotos))
    if len(unicos) > 10_000 or any(not _oid_git_valido(x) for x in unicos):
        return None
    args = ["rev-list", commit_oid]
    if unicos:
        args.extend(["--not", *unicos])
    return _git(".", *args, cwd_fd=repo_fd)


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


def _remoto_git_permitido(remoto):
    """Rechaza helpers/protocolos extensibles con un parser lineal cerrado."""
    if (not isinstance(remoto, str) or not remoto or len(remoto) > 4096
            or any(c in remoto for c in ("\x00", "\n", "\r"))):
        return False
    if os.path.isabs(remoto) or remoto.startswith("file:///"):
        return True

    minusculas = remoto.lower()
    if minusculas.startswith("https://"):
        autoridad, barra, ruta = remoto[8:].partition("/")
        return bool(
            barra and ruta and not any(c.isspace() for c in ruta)
            and _host_puerto_remoto_valido(autoridad)
        )
    if minusculas.startswith("ssh://"):
        autoridad, barra, ruta = remoto[6:].partition("/")
        if not barra or not ruta or any(c.isspace() for c in ruta):
            return False
        usuario, arroba, host_puerto = autoridad.partition("@")
        if arroba:
            if not _usuario_remoto_valido(usuario) or "@" in host_puerto:
                return False
        else:
            host_puerto = autoridad
        return _host_puerto_remoto_valido(host_puerto)

    usuario, arroba, destino = remoto.partition("@")
    host, dos_puntos, ruta = destino.partition(":")
    return bool(
        arroba and dos_puntos and _usuario_remoto_valido(usuario)
        and host and ruta and not any(c.isspace() or c in "/@:" for c in host)
        and not any(c.isspace() for c in ruta)
    )


def publicar_repo(carpeta, mensaje="chore: cambios verificados por Orquesta IA",
                  identidad_esperada=None, identidad_repo_esperada=None,
                  snapshot_esperado=None):
    """Escanea, confirma y publica un repo sin permitir credenciales.

    Se revisan el arbol completo, los blobs staged y cualquier commit local que
    aun no exista en el remoto. Nunca se hace force-push ni merge automatico.
    """
    def coincide(fd, esperada):
        if esperada is None:
            return True
        if (not isinstance(esperada, (list, tuple)) or len(esperada) != 2
                or any(type(x) is not int for x in esperada)):
            return False
        estado = os.fstat(fd)
        return tuple(esperada) == (estado.st_dev, estado.st_ino)

    if snapshot_esperado is not None:
        if not _snapshot_valido(snapshot_esperado):
            return {"ok": False, "fase": "seguridad",
                    "detalle": "snapshot verificado invalido"}
        if (identidad_esperada != snapshot_esperado["cwd_identidad"]
                or identidad_repo_esperada != snapshot_esperado["repo_identidad"]):
            return {"ok": False, "fase": "seguridad",
                    "detalle": "identidades no corresponden al snapshot"}
    elif identidad_esperada is not None or identidad_repo_esperada is not None:
        return {"ok": False, "fase": "seguridad",
                "detalle": "la verificacion no fijo un snapshot inmutable"}
    try:
        # Se adquiere primero exactamente el cwd verificado. Desde él se deriva
        # el top-level ascendiendo por descriptores, sin abrir la ruta textual
        # que Git pudiera devolver ni confundir un subdirectorio con la raíz.
        with _capacidad_trabajo(carpeta) as (_, cwd_fd):
            if not coincide(cwd_fd, identidad_esperada):
                return {"ok": False, "fase": "seguridad",
                        "detalle": "la carpeta no es el inode verificado"}
            with _capacidad_raiz_git(cwd_fd) as (raiz, repo_fd, _prefijo):
                if not coincide(repo_fd, identidad_repo_esperada):
                    return {"ok": False, "fase": "seguridad",
                            "detalle": "el repositorio no es el inode verificado"}
                return _publicar_repo_adquirido(
                    raiz, mensaje, repo_fd, snapshot_esperado=snapshot_esperado
                )
    except OSError:
        return {"ok": False, "fase": "seguridad",
                "detalle": "la carpeta o raiz Git cambio o dejo de ser segura"}


def _publicar_repo_adquirido(raiz, mensaje, repo_fd, snapshot_esperado=None):
    """Completa la transacción usando siempre el mismo descriptor de repo."""
    def git(*args, timeout=120):
        return _git(".", *args, timeout=timeout, cwd_fd=repo_fd)

    def push_oid_exacto(oid, oid_remoto_esperado):
        ref_destino = f"refs/heads/{rama}"
        refspec = f"{oid}:{ref_destino}"
        lease = f"--force-with-lease={ref_destino}:{oid_remoto_esperado or ''}"
        return git("push", lease, "--", remoto_push, refspec, timeout=300)

    def leer_tracking_local(oid):
        ref = f"refs/remotes/origin/{rama}"
        actual = git(
            "for-each-ref", "--format=%(refname)%09%(objectname)", ref
        )
        if not actual or actual.returncode != 0:
            return None
        lineas = actual.stdout.splitlines()
        if not lineas:
            return "0" * len(oid)
        if len(lineas) != 1:
            return None
        partes = lineas[0].split("\t")
        if (len(partes) != 2 or partes[0] != ref
                or not _oid_git_valido(partes[1])
                or len(partes[1]) != len(oid)):
            return None
        return partes[1]

    def registrar_tracking(existia_remota, oid, creado, oid_anterior):
        # El push usa una URL literal, por lo que no actualiza el ref de
        # seguimiento. Lo movemos con CAS: un fetch concurrente gana y se
        # informa como fallo posterior al push en vez de pisarlo.
        seguimiento = git(
            "update-ref", f"refs/remotes/origin/{rama}", oid, oid_anterior
        )
        if not seguimiento or seguimiento.returncode != 0:
            return {
                "ok": False, "fase": "tracking", "publicado": True,
                "detalle": "el commit se publico, pero no pude registrar origin",
                "commit": oid[:12], "creado": creado, "raiz": raiz,
            }
        if existia_remota:
            return None
        tracking = git(
            "branch", f"--set-upstream-to=origin/{rama}", "--", rama
        )
        if tracking and tracking.returncode == 0:
            return None
        # El push exacto ya terminó: un fallo local de tracking no permite
        # afirmar que el commit no se publicó ni volver a empujarlo a ciegas.
        return {
            "ok": False, "fase": "tracking", "publicado": True,
            "detalle": "el commit se publico, pero no pude configurar upstream",
            "commit": oid[:12], "creado": creado, "raiz": raiz,
        }

    ejecutable = _config_git_ejecutable(".", cwd_fd=repo_fd)
    if ejecutable is None:
        return {"ok": False, "fase": "seguridad",
                "detalle": "no pude auditar la configuracion ejecutable de Git"}
    if ejecutable:
        return {"ok": False, "fase": "seguridad",
                "detalle": "el repositorio declara filtros o drivers ejecutables"}
    rama_r = git("symbolic-ref", "--quiet", "--short", "HEAD")
    if not rama_r or rama_r.returncode != 0 or not rama_r.stdout.strip():
        return {"ok": False, "fase": "git", "detalle": "HEAD esta separado de una rama"}
    rama = rama_r.stdout.strip()
    remoto_r = git("remote", "get-url", "origin")
    if not remoto_r or remoto_r.returncode != 0:
        return {"ok": False, "fase": "git", "detalle": "falta el remoto origin"}
    remoto = remoto_r.stdout.strip()
    if not _remoto_git_permitido(remoto):
        return {"ok": False, "fase": "seguridad",
                "detalle": "origin usa credenciales, helper o protocolo no permitido"}
    remoto_push_r = git("remote", "get-url", "--push", "origin")
    remoto_push = remoto_push_r.stdout.strip() if remoto_push_r else ""
    if (not remoto_push_r or remoto_push_r.returncode != 0
            or not _remoto_git_permitido(remoto_push)):
        return {"ok": False, "fase": "seguridad",
                "detalle": "pushurl de origin no es un remoto permitido"}

    try:
        # La comparacion debe describir exactamente el destino de push; un
        # pushurl puede diferir deliberadamente del URL de fetch de origin.
        refs_remotas = _refs_remotas_confiables(repo_fd, remoto_push)
        testigo_historial = _testigo_historial_git(repo_fd)
    except OSError:
        return {"ok": False, "fase": "fetch",
                "detalle": "no pude adquirir refs e historial confiables de origin"}
    oids_remotos = tuple(dict.fromkeys(refs_remotas.values()))

    if snapshot_esperado is not None:
        snapshot = snapshot_esperado
        if rama != snapshot["rama"]:
            return {"ok": False, "fase": "seguridad",
                    "detalle": "la rama cambio desde la verificacion"}
        if snapshot["base_oid"] is None:
            ref_local = git(
                "show-ref", "--verify", "--quiet", f"refs/heads/{rama}"
            )
            if not ref_local or ref_local.returncode != 1:
                return {"ok": False, "fase": "seguridad",
                        "detalle": "la rama unborn cambio desde la verificacion"}
        else:
            head_r = git("rev-parse", "HEAD")
            head_oid = (head_r.stdout.strip()
                        if head_r and head_r.returncode == 0 else "")
            if head_oid != snapshot["base_oid"]:
                return {"ok": False, "fase": "seguridad",
                        "detalle": "HEAD cambio desde la verificacion"}
        tree_commit_r = git(
            "rev-parse", f"{snapshot['commit_oid']}^{{tree}}"
        )
        if (not tree_commit_r or tree_commit_r.returncode != 0
                or tree_commit_r.stdout.strip() != snapshot["tree_oid"]):
            return {"ok": False, "fase": "seguridad",
                    "detalle": "el commit probado no coincide con su tree"}
        try:
            tree_vivo = _tree_publicable_fd(repo_fd, snapshot["base_oid"])
        except OSError:
            return {"ok": False, "fase": "seguridad",
                    "detalle": "no pude revalidar el contenido probado"}
        if tree_vivo != snapshot["tree_oid"]:
            return {"ok": False, "fase": "seguridad",
                    "detalle": "el contenido cambio despues de las pruebas"}

        # Poblar el índice desde el tree inmutable evita volver a ejecutar
        # filtros clean/process aunque la configuración cambie en carrera.
        add = git("read-tree", snapshot["tree_oid"], timeout=180)
        if not add or add.returncode != 0:
            return {"ok": False, "fase": "stage",
                    "detalle": "no pude fijar el indice en el tree probado"}
        tree_stage_r = git("write-tree")
        tree_stage = (tree_stage_r.stdout.strip()
                      if tree_stage_r and tree_stage_r.returncode == 0 else "")
        if tree_stage != snapshot["tree_oid"]:
            return {"ok": False, "fase": "seguridad",
                    "detalle": "el contenido cambio durante la congelacion"}
        if _config_git_ejecutable(".", cwd_fd=repo_fd) is not False:
            return {"ok": False, "fase": "seguridad",
                    "detalle": "la configuracion Git cambio durante la publicacion"}

        oid_rama_remota = refs_remotas.get(rama)
        existia_remota = oid_rama_remota is not None
        if snapshot["base_oid"] is None and existia_remota:
            return {"ok": False, "fase": "sincronizacion",
                    "detalle": "origin ya contiene la rama unborn verificada"}
        if existia_remota:
            cuenta = git(
                "rev-list", "--left-right", "--count",
                f"{snapshot['base_oid']}...{oid_rama_remota}",
            )
            if not cuenta or cuenta.returncode != 0:
                return {"ok": False, "fase": "git",
                        "detalle": "no pude comparar la base verificada con origin"}
            try:
                _delante, detras = (int(x) for x in cuenta.stdout.split())
            except (ValueError, TypeError):
                return {"ok": False, "fase": "git",
                        "detalle": "comparacion remota invalida"}
            if detras:
                return {"ok": False, "fase": "sincronizacion",
                        "detalle": "origin tiene cambios nuevos; integra antes de publicar"}

        try:
            testigo_snapshot = _testigo_historial_git(repo_fd)
        except OSError:
            return {"ok": False, "fase": "seguridad",
                    "detalle": "historial del snapshot no reproducible"}
        finales_r = _rev_list_excluyendo_refs(
            repo_fd, snapshot["commit_oid"], oids_remotos
        )
        if not finales_r or finales_r.returncode != 0:
            return {"ok": False, "fase": "git",
                    "detalle": "no pude enumerar los commits del snapshot"}
        finales = [x for x in finales_r.stdout.splitlines() if x]
        if (len(finales) > 10_000
                or any(not _oid_git_valido(x) for x in finales)):
            return {"ok": False, "fase": "seguridad",
                    "detalle": "lista final de commits invalida o excesiva"}
        por_escanear = list(dict.fromkeys(finales + [snapshot["commit_oid"]]))
        if any(not _escanear_commit_fd(repo_fd, oid) for oid in por_escanear):
            return {"ok": False, "fase": "seguridad",
                    "detalle": "un commit verificable no supero el escaneo"}
        try:
            if _testigo_historial_git(repo_fd) != testigo_snapshot:
                raise OSError("topologia Git inestable")
        except OSError:
            return {"ok": False, "fase": "seguridad",
                    "detalle": "la topologia Git cambio durante el escaneo"}

        oid_anterior = (snapshot["base_oid"] if snapshot["base_oid"] is not None
                        else "0" * len(snapshot["commit_oid"]))
        actualizado = git(
            "update-ref", f"refs/heads/{rama}", snapshot["commit_oid"],
            oid_anterior,
        )
        if not actualizado or actualizado.returncode != 0:
            return {"ok": False, "fase": "seguridad",
                    "detalle": "la rama cambio antes de fijar el commit probado"}
        tracking_anterior = leer_tracking_local(snapshot["commit_oid"])
        if tracking_anterior is None:
            return {"ok": False, "fase": "tracking",
                    "detalle": "no pude atestiguar el tracking local antes del push"}
        push = push_oid_exacto(snapshot["commit_oid"], oid_rama_remota)
        if not push or push.returncode != 0:
            return {"ok": False, "fase": "push",
                    "detalle": "el commit probado quedo local, pero git push fallo"}
        fallo_tracking = registrar_tracking(
            existia_remota, snapshot["commit_oid"], bool(snapshot.get("creado")),
            tracking_anterior,
        )
        if fallo_tracking:
            return fallo_tracking
        return {"ok": True, "fase": "listo",
                "detalle": "publicado exactamente el snapshot probado",
                "commit": snapshot["commit_oid"][:12],
                "creado": bool(snapshot.get("creado")), "raiz": raiz}

    oid_rama_remota = refs_remotas.get(rama)
    existia_remota = oid_rama_remota is not None
    head_inicial_r = git("rev-parse", "HEAD")
    head_inicial = (head_inicial_r.stdout.strip()
                    if head_inicial_r and head_inicial_r.returncode == 0 else "")
    unborn = not _oid_git_valido(head_inicial)
    if unborn:
        ref_local = git(
            "show-ref", "--verify", "--quiet", f"refs/heads/{rama}"
        )
        if not ref_local or ref_local.returncode != 1:
            return {"ok": False, "fase": "git",
                    "detalle": "HEAD local invalido"}
        if existia_remota:
            return {"ok": False, "fase": "sincronizacion",
                    "detalle": "origin ya contiene la rama unborn local"}
    commits_locales = []
    if existia_remota:
        cuenta = git("rev-list", "--left-right", "--count",
                     f"{head_inicial}...{oid_rama_remota}")
        if not cuenta or cuenta.returncode != 0:
            return {"ok": False, "fase": "git", "detalle": "no pude comparar con origin"}
        try:
            delante, detras = (int(x) for x in cuenta.stdout.split())
        except (ValueError, TypeError):
            return {"ok": False, "fase": "git", "detalle": "comparacion remota invalida"}
        if detras:
            return {"ok": False, "fase": "sincronizacion",
                    "detalle": "origin tiene cambios nuevos; integra antes de publicar"}
        lista = git("rev-list", f"{oid_rama_remota}..HEAD") if delante else None
    elif not unborn:
        # Una rama sin upstream puede contener secretos en commits anteriores
        # ya borrados del tip. Se enumeran siempre todos los objetos que ningún
        # ref remoto conoce antes de permitir el primer push.
        lista = _rev_list_excluyendo_refs(repo_fd, head_inicial, oids_remotos)
    else:
        lista = None
    if lista is not None:
        if not lista or lista.returncode != 0:
            return {"ok": False, "fase": "git", "detalle": "no pude auditar commits locales"}
        commits_locales = [x for x in lista.stdout.splitlines() if x]
        if (len(commits_locales) > 10_000
                or any(not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", x)
                       for x in commits_locales)):
            return {"ok": False, "fase": "seguridad",
                    "detalle": "lista de commits locales invalida o excesiva"}

    scanner = os.path.join(CODIGO_ORQUESTA, "tools", "scan-secretos.sh")
    if not os.path.isfile(scanner):
        return {"ok": False, "fase": "seguridad", "detalle": "falta el escaner de secretos"}

    def escanear(*opciones):
        argv = ["/usr/bin/bash", scanner, *opciones, "--repo", "."]
        out, err, rc = _ejecutar_aislado_acotado(
            argv, {"HOME": HOME_USUARIO, "PATH": "/usr/bin:/bin",
                   "LC_ALL": "C.UTF-8"}, ".", 180,
            limite_salida=2 * 1024 * 1024,
            limite_error=2 * 1024 * 1024,
            usar_scope=False, cwd_fd=repo_fd,
        )
        return subprocess.CompletedProcess(argv, rc, out, err)

    for commit in commits_locales:
        revisado = escanear("--commit", commit)
        if not revisado or revisado.returncode != 0:
            return {"ok": False, "fase": "seguridad",
                    "detalle": "un commit local no supero el escaneo de secretos"}
    completo = escanear("--todo")
    if not completo or completo.returncode != 0:
        return {"ok": False, "fase": "seguridad",
                "detalle": "el arbol de trabajo no supero el escaneo de secretos"}
    try:
        if _testigo_historial_git(repo_fd) != testigo_historial:
            raise OSError("topologia Git inestable")
    except OSError:
        return {"ok": False, "fase": "seguridad",
                "detalle": "la topologia Git cambio durante la auditoria inicial"}

    add = git("add", "-A")
    if not add or add.returncode != 0:
        return {"ok": False, "fase": "stage", "detalle": "git add fallo"}
    staged = escanear("--staged")
    if not staged or staged.returncode != 0:
        return {"ok": False, "fase": "seguridad",
                "detalle": "el indice no supero el escaneo de secretos"}

    hay_stage = git("diff", "--cached", "--quiet")
    creado = False
    if hay_stage is None:
        return {"ok": False, "fase": "git", "detalle": "no pude leer el indice"}
    if hay_stage.returncode == 1:
        commit = git("commit", "-m", str(mensaje)[:200], timeout=180)
        if not commit or commit.returncode != 0:
            return {"ok": False, "fase": "commit", "detalle": "git commit fallo"}
        creado = True
        ultimo = git("rev-parse", "HEAD")
        if not ultimo or ultimo.returncode != 0:
            return {"ok": False, "fase": "git", "detalle": "no pude verificar el commit"}
        revisado = escanear("--commit", ultimo.stdout.strip())
        if not revisado or revisado.returncode != 0:
            return {"ok": False, "fase": "seguridad",
                    "detalle": "el commit nuevo no supero el escaneo"}
    elif hay_stage.returncode != 0:
        return {"ok": False, "fase": "git", "detalle": "estado del indice invalido"}

    # Congela el objeto exacto después de cualquier commit propio y vuelve a
    # enumerar sus ancestros no presentes en remotos. La rama local es mutable;
    # nunca se usa como fuente del push después de esta auditoría final.
    final_r = git("rev-parse", "HEAD")
    final_oid = final_r.stdout.strip() if final_r and final_r.returncode == 0 else ""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", final_oid):
        return {"ok": False, "fase": "git", "detalle": "HEAD final invalido"}
    if existia_remota:
        # HEAD pudo cambiar después de la comparación inicial. La evidencia
        # final se ata otra vez al OID remoto inventariado, sin resolver refs
        # mutables ni consultar commit-graph/replace/grafts locales.
        ancestro = git(
            "merge-base", "--is-ancestor", oid_rama_remota, final_oid
        )
        if not ancestro or ancestro.returncode != 0:
            return {"ok": False, "fase": "sincronizacion",
                    "detalle": "el OID final ya no desciende del tip remoto auditado"}
    try:
        testigo_final = _testigo_historial_git(repo_fd)
    except OSError:
        return {"ok": False, "fase": "seguridad",
                "detalle": "historial final no reproducible"}
    finales_r = _rev_list_excluyendo_refs(repo_fd, final_oid, oids_remotos)
    if not finales_r or finales_r.returncode != 0:
        return {"ok": False, "fase": "git",
                "detalle": "no pude congelar los commits a publicar"}
    finales = [x for x in finales_r.stdout.splitlines() if x]
    if (len(finales) > 10_000
            or any(not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", x)
                   for x in finales)):
        return {"ok": False, "fase": "seguridad",
                "detalle": "cobertura final de commits invalida o excesiva"}
    # Si una rama nueva nace de un tip ya alcanzable desde otro ref remoto,
    # ``rev-list`` con OIDs remotos puede ser correctamente vacío. El tip se
    # escanea de todas formas antes de crear el nuevo ref remoto.
    for commit_oid in dict.fromkeys(finales + [final_oid]):
        revisado = escanear("--commit", commit_oid)
        if not revisado or revisado.returncode != 0:
            return {"ok": False, "fase": "seguridad",
                    "detalle": "un commit final no supero el escaneo"}
    try:
        if _testigo_historial_git(repo_fd) != testigo_final:
            raise OSError("topologia Git inestable")
    except OSError:
        return {"ok": False, "fase": "seguridad",
                "detalle": "la topologia Git cambio durante el escaneo final"}

    tracking_anterior = leer_tracking_local(final_oid)
    if tracking_anterior is None:
        return {"ok": False, "fase": "tracking",
                "detalle": "no pude atestiguar el tracking local antes del push"}
    push = push_oid_exacto(final_oid, oid_rama_remota)
    if not push or push.returncode != 0:
        return {"ok": False, "fase": "push",
                "detalle": "el commit es local y seguro, pero git push fallo"}
    fallo_tracking = registrar_tracking(
        existia_remota, final_oid, creado, tracking_anterior
    )
    if fallo_tracking:
        return fallo_tracking
    return {"ok": True, "fase": "listo", "detalle": "publicado sin secretos",
            "commit": final_oid[:12],
            "creado": creado, "raiz": raiz}

# ---------------- USO REAL (todas las sesiones, no solo las de Orquesta) ----------------
# El ledger de Orquesta solo ve lo que Orquesta gasta. Pero tus sesiones
# manuales (claude, codex --yolo, agy) consumen de la MISMA cuota. Aqui se
# leen los registros que cada CLI deja en disco para ver el consumo real.
CACHE_REAL = os.path.join(BASE, "state", "uso_real.json")
USO_CACHE_VERSION = 3


def _dirs_sesiones(pid, p):
    prov = p.get("provider")
    if prov == "claude":
        ruta = ruta_cuenta(pid, p, predeterminado="projects")
        return [ruta] if ruta else []
    if prov == "gpt":
        ruta = ruta_cuenta(pid, p, predeterminado="sessions")
        return [ruta] if ruta else []
    if prov == "antigravity":
        ruta = ruta_cuenta(pid, p, predeterminado="conversations")
        return [ruta] if ruta else []
    return []


def _archivos_regulares(raiz, acepta, limite=10_000, profundidad_maxima=16,
                        con_identidad=False):
    """Enumera con capacidades ``dir_fd`` y límites contra árboles hostiles."""
    try:
        limite = max(0, int(limite))
        profundidad_maxima = max(0, int(profundidad_maxima))
    except (TypeError, ValueError):
        return []
    if not limite:
        return []
    base = os.path.abspath(_expandir_usuario(raiz))
    archivos = []
    directorios_vistos = set()
    archivos_vistos = set()
    presupuesto = [limite]

    flags_dir = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags_dir |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags_dir |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags_dir |= os.O_NOFOLLOW
    flags_archivo = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags_archivo |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags_archivo |= os.O_NOFOLLOW

    def visitar(dir_fd, ruta_visible, profundidad):
        if presupuesto[0] <= 0 or len(archivos) >= limite:
            return
        try:
            estado_dir = os.fstat(dir_fd)
            identidad_dir = (estado_dir.st_dev, estado_dir.st_ino)
            if not stat.S_ISDIR(estado_dir.st_mode) or identidad_dir in directorios_vistos:
                return
            directorios_vistos.add(identidad_dir)
            with os.scandir(dir_fd) as entradas:
                for entrada in entradas:
                    if presupuesto[0] <= 0 or len(archivos) >= limite:
                        return
                    presupuesto[0] -= 1
                    try:
                        estado = entrada.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(estado.st_mode):
                        continue
                    ruta_hija = os.path.join(ruta_visible, entrada.name)
                    if stat.S_ISDIR(estado.st_mode):
                        if profundidad >= profundidad_maxima:
                            continue
                        try:
                            hijo_fd = os.open(entrada.name, flags_dir, dir_fd=dir_fd)
                        except OSError:
                            continue
                        try:
                            comprobado = os.fstat(hijo_fd)
                            if ((comprobado.st_dev, comprobado.st_ino)
                                    != (estado.st_dev, estado.st_ino)):
                                continue
                            visitar(hijo_fd, ruta_hija, profundidad + 1)
                        finally:
                            os.close(hijo_fd)
                        continue
                    if not stat.S_ISREG(estado.st_mode) or not acepta(entrada.name):
                        continue
                    try:
                        archivo_fd = os.open(entrada.name, flags_archivo, dir_fd=dir_fd)
                    except OSError:
                        continue
                    try:
                        comprobado = os.fstat(archivo_fd)
                        identidad = (comprobado.st_dev, comprobado.st_ino)
                        if (not stat.S_ISREG(comprobado.st_mode)
                                or identidad != (estado.st_dev, estado.st_ino)
                                or identidad in archivos_vistos
                                or comprobado.st_uid != os.getuid()
                                or comprobado.st_nlink != 1):
                            continue
                        archivos_vistos.add(identidad)
                        firma = _identidad_estado(comprobado)
                        archivos.append((firma if con_identidad else comprobado.st_mtime,
                                         ruta_hija))
                    finally:
                        os.close(archivo_fd)
        except OSError:
            return

    try:
        with _abrir_directorio_seguro(base) as base_fd:
            visitar(base_fd, base, 0)
    except OSError:
        return []
    return archivos


def _claves_tiempo_local(ts):
    """Convierte un timestamp ISO (incluido ``Z``/UTC) a fecha y hora locales."""
    if not ts:
        return "", ""
    try:
        d = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is not None:
            d = d.astimezone()
        return d.strftime("%Y-%m-%d"), d.strftime("%Y-%m-%dT%H")
    except (TypeError, ValueError):
        return str(ts)[:10], str(ts)[:13]


def _uso_archivo_claude(f, identidad=None):
    """Suma el uso de una sesion de Claude Code. Devuelve (por_dia, por_hora)."""
    dias, horas = {}, {}
    try:
        with _abrir_regular_identidad(
                f, identidad, errors="ignore") as fh:
            for linea in _lineas_acotadas(fh, bytes_totales=64 * 1024 * 1024):
                if '"usage"' not in linea:
                    continue
                try:
                    d = json.loads(linea)
                except json.JSONDecodeError:
                    continue
                u = (d.get("message") or {}).get("usage") or d.get("usage")
                if not isinstance(u, dict):
                    continue
                ent = u.get("input_tokens", 0) or 0
                sal = u.get("output_tokens", 0) or 0
                cache = (u.get("cache_read_input_tokens", 0) or 0) + \
                        (u.get("cache_creation_input_tokens", 0) or 0)
                dia, hora = _claves_tiempo_local(d.get("timestamp"))
                for k, dic in ((dia, dias), (hora, horas)):
                    if not k:
                        continue
                    a = dic.setdefault(k, {"entrada": 0, "salida": 0, "cache": 0, "msgs": 0})
                    a["entrada"] += ent; a["salida"] += sal
                    a["cache"] += cache; a["msgs"] += 1
    except (OSError, UnicodeError):
        pass
    return dias, horas


def _uso_archivo_codex(f, identidad=None):
    dias, horas = {}, {}
    ult = 0
    ts_ult = ""
    uso_ult = {}
    try:
        with _abrir_regular_identidad(
                f, identidad, errors="ignore") as fh:
            for linea in _lineas_acotadas(fh, bytes_totales=64 * 1024 * 1024):
                if "token_usage" not in linea and "total_token" not in linea:
                    continue
                try:
                    d = json.loads(linea)
                except json.JSONDecodeError:
                    continue
                pl = d.get("payload") or d
                info = pl.get("info") if isinstance(pl.get("info"), dict) else {}
                u = info.get("total_token_usage") or pl.get("token_usage")
                if not isinstance(u, dict):
                    continue
                tot = u.get("total_tokens") or sum(
                    v for k, v in u.items() if isinstance(v, int) and k != "total_tokens")
                if tot and tot >= ult:          # el registro es acumulativo
                    ult = tot
                    uso_ult = u
                    ts_ult = d.get("timestamp") or pl.get("timestamp") or ts_ult
    except (OSError, UnicodeError):
        pass
    if ult and ts_ult:
        dia, hora = _claves_tiempo_local(ts_ult)
        ent = (uso_ult.get("input_tokens") or uso_ult.get("input_token_count") or 0)
        sal = (uso_ult.get("output_tokens") or uso_ult.get("output_token_count") or 0)
        cache = (uso_ult.get("cached_input_tokens") or
                 uso_ult.get("cache_read_input_tokens") or 0)
        if not (ent or sal or cache):
            ent = ult
        for k, dic in ((dia, dias), (hora, horas)):
            dic[k] = {"entrada": ent, "salida": sal, "cache": cache, "msgs": 1}
    return dias, horas


def uso_real(pid, p, refrescar=False):
    """Consumo real leyendo los registros, con cache idempotente por mtime."""
    prov = p.get("provider")
    cache = _leer(CACHE_REAL, {})
    entrada = cache.get(pid, {})
    archivos = {}
    for d in _dirs_sesiones(pid, p):
        for firma, ruta in _archivos_regulares(
                d, lambda nombre: nombre.endswith(".jsonl"), con_identidad=True):
            archivos[ruta] = firma
    if (not refrescar and entrada.get("version") == USO_CACHE_VERSION
            and entrada.get("archivos") == archivos):
        return {"dias": entrada.get("dias", {}), "horas": entrada.get("horas", {}),
                "archivos": len(archivos)}

    # Si un JSONL crece se relee el conjunto completo. Sumar el archivo nuevo
    # sobre su agregado anterior duplicaba todos los eventos ya contabilizados.
    dias, horas = {}, {}
    for ruta, firma in archivos.items():
        d1, h1 = (_uso_archivo_claude(ruta, firma) if prov == "claude"
                  else _uso_archivo_codex(ruta, firma) if prov == "gpt" else ({}, {}))
        for src, dst in ((d1, dias), (h1, horas)):
            for k, v in src.items():
                a = dst.setdefault(k, {"entrada": 0, "salida": 0, "cache": 0, "msgs": 0})
                for kk in ("entrada", "salida", "cache", "msgs"):
                    a[kk] += v.get(kk, 0)
    with bloqueo():
        c = _leer(CACHE_REAL, {})
        c[pid] = {"version": USO_CACHE_VERSION, "dias": dias, "horas": horas,
                  "archivos": archivos,
                  "actualizado": ahora().isoformat(timespec="seconds")}
        _escribir(CACHE_REAL, c)
    return {"dias": dias, "horas": horas, "archivos": len(archivos)}


def uso_real_ventana(pid, p, horas_ventana=5):
    """Tokens reales consumidos dentro de la ventana de recarga vigente."""
    r = uso_real(pid, p)
    corte = ahora() - datetime.timedelta(hours=horas_ventana)
    tot = {"entrada": 0, "salida": 0, "cache": 0, "msgs": 0}
    for hk, v in r["horas"].items():
        try:
            t = datetime.datetime.strptime(hk, "%Y-%m-%dT%H")
        except ValueError:
            continue
        if t >= corte.replace(minute=0, second=0, microsecond=0):
            for k in tot:
                tot[k] += v.get(k, 0)
    tot["facturable"] = tot["entrada"] + tot["salida"]
    tot["bruto"] = tot["facturable"] + tot["cache"]
    return tot

# ---------------- CUOTA REAL DEL PROVEEDOR ----------------
# Codex escribe su 'rate_limits' (used_percent real) en cada sesion.
# Claude no lo hace: ahi se estima desde los registros de sesion locales.
TIER_CLAUDE = {"default_claude_max_20x": ("Max 20x", 20),
               "default_claude_max_5x": ("Max 5x", 5),
               "default_claude_pro": ("Pro", 1)}


def cuota_codex(pid, p):
    """Lee el ultimo rate_limits que dejo codex: es cuota REAL del proveedor."""
    d = ruta_cuenta(pid, p, predeterminado="sessions")
    if not d:
        return None
    archivos = _archivos_regulares(
        d, lambda nombre: nombre.startswith("rollout-") and nombre.endswith(".jsonl"),
        con_identidad=True,
    )
    archivos.sort(reverse=True)
    for firma, ruta in archivos[:6]:
        ult = None
        try:
            with _abrir_regular_identidad(
                    ruta, firma, errors="ignore") as fh:
                for linea in _lineas_acotadas(fh, bytes_totales=64 * 1024 * 1024):
                    if "rate_limits" not in linea:
                        continue
                    try:
                        d2 = json.loads(linea)
                    except json.JSONDecodeError:
                        continue
                    rl = (d2.get("payload") or {}).get("rate_limits")
                    if rl:
                        ult = (d2.get("timestamp"), rl)
        except (OSError, UnicodeError):
            continue
        if ult:
            ts, rl = ult
            pr = rl.get("primary") or {}
            res = pr.get("resets_at")
            return {"fuente": "proveedor", "usado_pct": pr.get("used_percent"),
                    "ventana_min": pr.get("window_minutes"),
                    "reinicia": datetime.datetime.fromtimestamp(res).isoformat(timespec="minutes")
                                if res else None,
                    "plan": rl.get("plan_type"), "medido": ts[:19] if ts else None,
                    "creditos": (rl.get("credits") or {}).get("balance")}
    return None


def plan_claude(pid, p):
    """Nivel real del plan segun el token guardado por el CLI."""
    home = home_de(pid, p)
    rutas = [ruta_cuenta(pid, p, predeterminado=".claude.json")]
    home_claude = os.path.realpath(os.path.join(HOME_USUARIO, ".claude"))
    if home == home_claude:
        rutas.append(_ruta_sin_enlaces(
            HOME_USUARIO, os.path.join(HOME_USUARIO, ".claude.json")
        ))
    for ruta in rutas:
        if not ruta or not os.path.isfile(ruta) or os.path.islink(ruta):
            continue
        d = _leer(ruta, None)
        if not isinstance(d, dict):
            continue
        oa = d.get("oauthAccount") or {}
        t = oa.get("organizationRateLimitTier") or oa.get("userRateLimitTier")
        if t:
            nom, mult = TIER_CLAUDE.get(t, (t, 1))
            return {"tier": t, "nombre": nom, "multiplicador": mult,
                    "correo": oa.get("emailAddress")}
    try:
        credencial = ruta_cuenta(pid, p, predeterminado=".credentials.json")
        if not credencial or not os.path.isfile(credencial) or os.path.islink(credencial):
            return None
        cr = _leer(credencial, {})
        t = (cr.get("claudeAiOauth") or {}).get("rateLimitTier")
        if t:
            nom, mult = TIER_CLAUDE.get(t, (t, 1))
            return {"tier": t, "nombre": nom, "multiplicador": mult}
    except Exception:
        pass
    return None


def cuota(pid, p):
    """Mejor estimacion disponible del consumo de esa cuenta.

    'fuente' dice de donde sale: 'proveedor' es dato oficial; 'local' es
    calculado desde los registros de sesion de esta maquina, y por tanto
    NO incluye lo que uses desde el movil, la web u otro equipo.
    """
    prov = p.get("provider")
    if prov == "gpt":
        c = cuota_codex(pid, p)
        if c:
            return c
    horas = p.get("ventana_horas") or VENTANA_PLAN.get(p.get("plan", "desconocido"), 5)
    v = uso_real_ventana(pid, p, horas)
    out = {"fuente": "local", "ventana_horas": horas,
           "facturable": v["facturable"], "bruto": v["bruto"], "mensajes": v["msgs"]}
    if prov == "claude":
        pl = plan_claude(pid, p)
        if pl:
            out["plan_real"] = pl["nombre"]
            out["multiplicador"] = pl["multiplicador"]
            cupo = p.get("cupo_ventana") or 0
            if cupo:
                out["usado_pct"] = round(100 * v["facturable"] / cupo, 1)
    else:
        cupo = p.get("cupo_ventana") or 0
        if cupo:
            out["usado_pct"] = round(100 * v["facturable"] / cupo, 1)
    return out

# ---------------- CONOCIMIENTO DEL EQUIPO ----------------
# Orquesta controla la maquina entera. Esto es lo que sabe de ella siempre.
CTX_PC = os.path.join(BASE, "state", "equipo.json")


def _cmd(args, t=6):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=t)
        return r.stdout.strip()
    except Exception:
        return ""


def escanear_equipo():
    """Radiografia de la maquina: se cachea y se refresca cada 12 h."""
    d = {"generado": ahora().isoformat(timespec="seconds")}
    so = {}
    for linea in _cmd(["cat", "/etc/os-release"]).splitlines():
        if "=" in linea:
            k, v = linea.split("=", 1)
            so[k] = v.strip('"')
    d["so"] = so.get("PRETTY_NAME", "?")
    d["kernel"] = _cmd(["uname", "-r"])
    d["equipo"] = _cmd(["hostname"])
    d["usuario"] = os.environ.get("USER", "")
    d["escritorio"] = os.environ.get("XDG_CURRENT_DESKTOP", "") + " / " + \
                      os.environ.get("XDG_SESSION_TYPE", "")
    cpu = [l.split(":", 1)[1].strip() for l in _cmd(["lscpu"]).splitlines()
           if l.startswith("Model name") or l.startswith("Nombre del modelo")]
    d["cpu"] = cpu[0] if cpu else "?"
    d["nucleos"] = _cmd(["nproc"])
    mem = _cmd(["free", "-h"]).splitlines()
    d["ram"] = mem[1].split()[1] if len(mem) > 1 else "?"
    d["gpu"] = [l.split(": ", 1)[-1] for l in _cmd(["lspci"]).splitlines()
                if "VGA" in l or "3D controller" in l]
    disco = _cmd(["df", "-h", "/"]).splitlines()
    d["disco"] = f"{disco[1].split()[3]} libres de {disco[1].split()[1]}" if len(disco) > 1 else "?"
    # herramientas relevantes
    herr = {}
    for h in ("git", "python3", "node", "npm", "docker", "ffmpeg", "kdenlive",
              "gh", "psql", "code", "kitty", "fastfetch", "rg", "jq"):
        r = shutil.which(h)
        if r:
            herr[h] = r
    d["herramientas"] = herr
    # carpetas del usuario
    home = HOME_USUARIO
    d["carpetas"] = [x for x in sorted(os.listdir(home))
                     if not x.startswith(".") and os.path.isdir(os.path.join(home, x))]
    proyectos = os.path.join(home, "Documentos")
    if os.path.isdir(proyectos):
        d["documentos"] = sorted(os.listdir(proyectos))[:25]
    with bloqueo():
        _escribir(CTX_PC, d)
    return d


def equipo(max_horas=12):
    d = _leer(CTX_PC, None)
    if d:
        try:
            g = datetime.datetime.fromisoformat(d["generado"])
            if (ahora() - g).total_seconds() < max_horas * 3600:
                return d
        except Exception:
            pass
    return escanear_equipo()


def contexto_equipo():
    """Texto compacto que se inyecta para que las IA conozcan la maquina."""
    d = equipo()
    cuentas = [f"{k} ({v.get('provider')})" for k, v in cfg().get("profiles", {}).items()
               if autenticado(k, v) and v.get("enabled", True)]
    return (
        f"Equipo que controlas (acceso total, permisos concedidos):\n"
        f"- {d.get('so')} · kernel {d.get('kernel')} · {d.get('escritorio')}\n"
        f"- {d.get('cpu')} ({d.get('nucleos')} nucleos) · {d.get('ram')} RAM · {d.get('disco')}\n"
        f"- GPU: {'; '.join(d.get('gpu') or []) or '?'}\n"
        f"- Usuario {d.get('usuario')} en {d.get('equipo')}; home /home/{d.get('usuario')}\n"
        f"- Carpetas: {', '.join(d.get('carpetas') or [])}\n"
        f"- Herramientas: {', '.join(sorted((d.get('herramientas') or {}).keys()))}\n"
        f"- IA conectadas por Orquesta: {', '.join(cuentas)}\n")

# ---------------- ESTADO VIVO DEL PROYECTO ----------------
# Cada IA ve, dentro de su prompt, que esta terminado, que esta en curso en
# este momento y quien lo hace. Asi no duplican trabajo ni pisan archivos.
class Pizarra:
    def __init__(self, carpeta, plan, contexto_terminal=""):
        self.carpeta = carpeta
        self.plan = plan
        self.ctx_term = contexto_terminal
        self.hechas = []          # [{id,titulo,perfil,resumen,archivos}]
        self.fallidas = []        # no se presentan a dependientes como terminadas
        self.en_curso = {}        # id -> {titulo, perfil, desde}
        self.lock = threading.Lock()

    def empezar(self, t, perfil):
        with self.lock:
            self.en_curso[t["id"]] = {"titulo": t["titulo"], "perfil": perfil,
                                      "desde": time.time()}

    def terminar(self, t, perfil, resumen, archivos=None):
        with self.lock:
            self.en_curso.pop(t["id"], None)
            self.hechas.append({"id": t["id"], "titulo": t["titulo"],
                                "perfil": perfil, "resumen": (resumen or "")[:300],
                                "archivos": archivos or t.get("archivos") or []})

    def fallar(self, t, perfil, resumen):
        with self.lock:
            self.en_curso.pop(t["id"], None)
            self.fallidas.append({"id": t["id"], "titulo": t["titulo"],
                                  "perfil": perfil, "resumen": (resumen or "")[:300]})

    def relevar(self, t, perfil, resumen):
        """Cierra el escritor fallido sin declarar fallida la tarea recuperable."""
        with self.lock:
            self.en_curso.pop(t["id"], None)

    def foto(self, excluir_id=None):
        """Texto que se inyecta a quien esta trabajando ahora mismo."""
        with self.lock:
            partes = []
            if self.ctx_term:
                partes.append(f"Contexto de la conversacion con el usuario:\n{self.ctx_term}")
            if self.hechas:
                partes.append("YA TERMINADO (no lo rehagas, integra con ello):\n" +
                              "\n".join(f"- [{h['perfil']}] {h['titulo']}"
                                         + (f" -> {', '.join(h['archivos'])}" if h["archivos"] else "")
                                         + (f": {h['resumen'][:160]}" if h["resumen"] else "")
                                         for h in self.hechas))
            if self.fallidas:
                partes.append("FALLIDO O BLOQUEADO (no lo des por terminado):\n" +
                              "\n".join(f"- [{h['perfil']}] {h['titulo']}: "
                                        f"{h['resumen'][:160]}"
                                        for h in self.fallidas))
            en_curso = {k: v for k, v in self.en_curso.items() if k != excluir_id}
            if en_curso:
                partes.append("EN CURSO AHORA MISMO por otra IA (NO toques esos archivos):\n" +
                              "\n".join(f"- [{v['perfil']}] {v['titulo']}"
                                         for v in en_curso.values()))
            return "\n\n".join(partes)

    def archivos_reales(self):
        try:
            return sorted(f for f in os.listdir(self.carpeta) if not f.startswith("."))
        except OSError:
            return []


DELIBERAR = """Eres uno de varios modelos que van a construir esto en equipo.
Encargo: {encargo}

Plan propuesto:
{plan}

Modelos disponibles y en que destaca cada uno:
{cuentas}

Responde SOLO un JSON:
{{"asignacion":{{"t1":"nombre-de-cuenta", "t2":"..."}},
  "una_sola":false,
  "motivo":"una frase"}}

Criterios:
- "una_sola" es true SOLO si el trabajo es tan acoplado que repartirlo lo
  empeoraria; en ese caso asigna todas las tareas a la misma cuenta.
- Si repartir ayuda, reparte de verdad: no pongas todo en una sola cuenta.
- Respeta que una cuenta con poca cuota restante reciba menos carga."""

# ---------------- AUDITORIA CRUZADA SOBRE ARCHIVOS REALES ----------------
def _mas_potentes(n=3, tarea="review", preferir=None):
    """Las cuentas mas capaces con cuota, para que se auditen entre ellas."""
    return ranking(tarea, preferir=preferir)[:n]


def auditar_proyecto(plan, carpeta, resultados, timeout=600, callback=None,
                     arreglar=True, preferir=None):
    """Cada modelo fuerte revisa lo que escribieron los OTROS y lo corrige.

    No es una opinion sobre un texto: leen los archivos del disco, buscan
    defectos concretos y, si 'arreglar', los arreglan ahi mismo.
    """
    fuertes = _mas_potentes(3, preferir=preferir)
    if len(fuertes) < 2:
        return [], "hacen falta al menos 2 cuentas para auditarse entre si"

    # quien escribio que
    autoria = {}
    for r in resultados:
        if r.get("perfil"):
            autoria.setdefault(r["perfil"], []).append(r.get("titulo", ""))

    trabajos = []
    for x in fuertes:
        pid = x["pid"]
        ajenos = {k: v for k, v in autoria.items() if k != pid}
        if not ajenos:
            continue
        lista = "\n".join(f"- {k} hizo: {'; '.join(v)}" for k, v in ajenos.items())
        prompt = (
            f"Auditoria tecnica del proyecto en {carpeta}.\n"
            f"Encargo original: {plan.get('resumen','')}\n\n"
            f"Trabajo hecho por OTROS modelos (tu no lo escribiste):\n{lista}\n\n"
            f"Lee de verdad los archivos de esa carpeta y audita SOLO el trabajo "
            f"ajeno. Busca: errores que rompan la ejecucion, promesas del encargo "
            f"que no se cumplieron, inconsistencias entre piezas de distintos "
            f"autores, y calidad por debajo de lo pedido.\n"
            + (f"Corrige lo que encuentres directamente en los archivos.\n"
               if arreglar else "No modifiques nada, solo reporta.\n")
            + f"Responde en maximo 8 lineas: cada defecto con su archivo, y si lo "
              f"arreglaste o no. Si el trabajo ajeno esta bien, dilo en una linea "
              f"en vez de inventar problemas.")
        trabajos.append((pid, x["p"], prompt))

    if callback:
        callback("auditoria", {"cuentas": [t[0] for t in trabajos]})
    reservados = {t[0] for t in trabajos}

    def ejecutar(t):
        pid, perfil, prompt = t
        opciones_lectura = {"solo_lectura": True} if not arreglar else {}
        r = correr(pid, perfil, prompt, "review", timeout, carpeta,
                   **opciones_lectura)
        if r.get("rc") == 0:
            return pid, r
        usados = set(reservados)
        usados.add(pid)
        for alterna in ranking("review", preferir=preferir):
            if alterna["pid"] in usados:
                continue
            relevo = (
                prompt + "\n\nRELEVO CONTROLADO: el auditor anterior ya termino "
                f"con rc={r.get('rc')}. Revisa el estado actual y completa esta "
                "auditoria sin repetir ni deshacer correcciones utiles."
            )
            r = correr(alterna["pid"], alterna["p"], relevo, "review",
                       timeout, carpeta, **opciones_lectura)
            pid = alterna["pid"]
            usados.add(pid)
            if r.get("rc") == 0:
                break
        return pid, r
    if arreglar:
        # Dos revisores escribiendo a la vez pueden corregir el mismo archivo
        # de formas incompatibles. Las auditorias con cambios son deliberadamente
        # seriales; las de solo lectura conservan el paralelismo.
        salidas = [ejecutar(t) for t in trabajos]
    elif trabajos:
        with ThreadPoolExecutor(max_workers=len(trabajos)) as ex:
            salidas = list(ex.map(ejecutar, trabajos))
    else:
        salidas = []
    out = []
    for pid, r in salidas:
        out.append({"perfil": pid, "texto": (r["texto"] or "").strip(),
                    "tokens": r["tokens"], "seg": r["seg"], "rc": r["rc"],
                    "run_id": r.get("run_id")})
        if callback:
            callback("auditor", out[-1])
    return out, None

# ---------------- CUOTA GLOBAL DECLARADA A MANO ----------------
# Claude no publica el % consumido por ninguna via (lo verifiqué en la API,
# los archivos de sesion y el estado local). Codex si lo publica. Para las
# que no, el usuario declara el porcentaje que ve en la configuracion de uso.
MANUAL = os.path.join(BASE, "state", "cuota_manual.json")


def cuota_manual_leer():
    return _leer(MANUAL, {})


def cuota_manual_fijar(pid, pct, nota=""):
    pct = float(pct)
    if not 0 <= pct <= 100:
        raise ValueError("el porcentaje debe estar entre 0 y 100")
    with bloqueo():
        d = _leer(MANUAL, {})
        d[pid] = {"usado_pct": pct,
                  "declarado": ahora().isoformat(timespec="minutes"),
                  "nota": nota}
        _escribir(MANUAL, d)
    return d[pid]


def antiguedad_horas(iso):
    try:
        return (ahora() - datetime.datetime.fromisoformat(iso)).total_seconds() / 3600
    except Exception:
        return None


def cuota_global(pid, p):
    """Consumo global de la cuenta, en porcentaje, con su procedencia.

    fuente: 'proveedor' (dato oficial) | 'declarado' (lo dijo el usuario)
            | 'sin dato' (no hay forma de saberlo)
    """
    prov = p.get("provider")
    if prov == "gpt":
        c = cuota_codex(pid, p)
        if c and c.get("usado_pct") is not None:
            return {"pct": c["usado_pct"], "fuente": "proveedor",
                    "ventana_h": (c.get("ventana_min") or 0) / 60,
                    "reinicia": c.get("reinicia"), "plan": c.get("plan"),
                    "edad_h": 0}
    m = cuota_manual_leer().get(pid)
    if m:
        edad = antiguedad_horas(m["declarado"])
        ventana = p.get("ventana_horas") or VENTANA_PLAN.get(
            p.get("plan", "desconocido"), 5)
        base = {"declarado": m["declarado"], "nota": m.get("nota", ""),
                "edad_h": edad, "ventana_h": ventana,
                "pct_declarado": m["usado_pct"]}
        # Una medicion manual describe la ventana que estaba visible en ese
        # momento. Al cumplirse esa ventana ya no puede seguir bloqueando para
        # siempre una cuenta que se recargo.
        if edad is not None and edad >= ventana:
            return {"pct": None, "fuente": "caducado", **base}
        return {"pct": m["usado_pct"], "fuente": "declarado", **base}
    return {"pct": None, "fuente": "sin dato", "edad_h": None}
