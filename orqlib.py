"""Nucleo compartido del orquestador multi-cuenta.

Estado en disco con bloqueo fcntl: cualquier numero de terminales puede usar
el sistema a la vez sin corromper el ledger ni los contadores.
"""
import json, os, re, shlex, shutil, subprocess, sys, threading, time, datetime, fcntl, contextlib, uuid, signal, hashlib, glob
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

_SUBPROCESS_RUN_ORIGINAL = subprocess.run
_SYSTEMD_USUARIO_CACHE = {}

BASE = os.environ.get("ORQ_HOME") or os.path.dirname(os.path.abspath(__file__))
ACCOUNTS = os.path.join(BASE, "accounts")
PROFILES = os.path.join(BASE, "profiles.json")
LEDGER = os.path.join(BASE, "state", "ledger.jsonl")
LIMITS = os.path.join(BASE, "state", "limits.json")
SCORES = os.path.join(BASE, "state", "scores.json")
LOCK = os.path.join(BASE, "state", ".lock")
CLAVE_LIMITE_ANTIGRAVITY = "@antigravity-global"

# MiniMax habla el protocolo de Anthropic: reusamos el binario "claude"
# apuntandolo a su endpoint. Por eso nunca exportamos estas variables de
# forma global: pisarian la cuenta Claude real de las terminales.
MINIMAX_BASE_URL = "https://api.minimax.io/anthropic"
MINIMAX_BASE_URL_CN = "https://api.minimaxi.com/anthropic"
MINIMAX_MODELO = "MiniMax-M3[1m]"

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
MIN_MUESTRA_REPARTO = 10_000


def id_perfil_valido(pid):
    return bool(ID_PERFIL_RE.fullmatch(str(pid or "")))


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
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    f = open(LOCK, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _leer(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _escribir(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def cfg():
    return _leer(PROFILES, {"profiles": {}})


def guardar_cfg(c):
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
    if os.path.exists(LEDGER):
        with open(LEDGER) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    if dias:
        corte = (ahora() - datetime.timedelta(days=dias)).date().isoformat()
        rows = [r for r in rows if r.get("fecha", "") >= corte]
    return rows


def log(entry):
    with bloqueo():
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        with open(LEDGER, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


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
    h = p.get("home")
    if h:
        h = os.path.expanduser(h)
        return os.path.abspath(h if os.path.isabs(h) else os.path.join(BASE, h))
    return os.path.join(ACCOUNTS, pid)


def home_purgable(pid, p):
    """Solo permite purgar el directorio privado directo de ese id."""
    if not id_perfil_valido(pid):
        return False
    actual = os.path.realpath(home_de(pid, p))
    esperado = os.path.realpath(os.path.join(ACCOUNTS, pid))
    try:
        dentro = os.path.commonpath([actual, os.path.realpath(ACCOUNTS)]) \
            == os.path.realpath(ACCOUNTS)
    except ValueError:
        return False
    return dentro and actual == esperado


def entorno(pid, p):
    env = dict(os.environ)
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
        "GEMINI_CLI_TRUST_WORKSPACE",
    ):
        env.pop(nombre, None)
    if prov == "claude":
        env["CLAUDE_CONFIG_DIR"] = h
    elif prov == "gpt":
        env["CODEX_HOME"] = h
    elif prov == "minimax":
        env["CLAUDE_CONFIG_DIR"] = h
        env["ANTHROPIC_BASE_URL"] = p.get("base_url") or MINIMAX_BASE_URL
        k = p.get("api_key_file")
        if k and os.path.exists(os.path.expanduser(k)):
            with open(os.path.expanduser(k)) as archivo:
                env["ANTHROPIC_AUTH_TOKEN"] = archivo.read().strip()
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
        k = p.get("api_key_file")
        if p.get("auth") == "oauth":
            env["GOOGLE_GENAI_USE_GCA"] = "true"
        elif k and os.path.exists(os.path.expanduser(k)):
            with open(os.path.expanduser(k)) as archivo:
                env["GEMINI_API_KEY"] = archivo.read().strip()
    for k, v in (p.get("env") or {}).items():
        env[k] = v
    # Todo `git push` iniciado por una IA hereda un pre-push que escanea cada
    # commit. La publicacion normal de Orquesta vuelve a escanear por su cuenta;
    # esta capa evita que un modelo se salte accidentalmente el gate.
    hook = os.path.join(BASE, "tools", "git-hooks")
    if os.path.isfile(os.path.join(hook, "pre-push")):
        try:
            n_git = int(env.get("GIT_CONFIG_COUNT", "0"))
        except (TypeError, ValueError):
            n_git = 0
        env[f"GIT_CONFIG_KEY_{n_git}"] = "core.hooksPath"
        env[f"GIT_CONFIG_VALUE_{n_git}"] = hook
        env["GIT_CONFIG_COUNT"] = str(n_git + 1)
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
        os.path.expanduser(
            f"~/.config/google-chrome/*/Extensions/{extension_id}/*/manifest.json"
        ),
        os.path.expanduser(
            f"~/.config/chromium/*/Extensions/{extension_id}/*/manifest.json"
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
        return base + [prompt]
    if prov == "minimax":
        base = ["claude", "-p", "--output-format", "json"] + perm
        if solo_lectura:
            base += ["--permission-mode", "plan"]
        base += ["--model", modelo or MINIMAX_MODELO]
        return base + [prompt]
    if prov == "gpt":
        base = ["codex", "exec", "--skip-git-repo-check"] + perm
        if solo_lectura:
            base += ["--sandbox", "read-only"]
        if modelo:
            base += ["-m", modelo]
        if mx.get("reasoning"):
            base += ["-c", f'model_reasoning_effort="{mx["reasoning"]}"']
        return base + [prompt]
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
    if prov == "claude":
        return os.path.exists(os.path.join(h, ".credentials.json"))
    if prov == "gpt":
        return os.path.exists(os.path.join(h, "auth.json"))
    if prov == "minimax":
        k = p.get("api_key_file") or os.path.join(h, "api_key")
        k = os.path.expanduser(k)
        return os.path.exists(k) and os.path.getsize(k) > 0
    if prov == "antigravity":
        import shutil
        return shutil.which("agy") is not None
    if prov == "gemini":
        if p.get("auth") == "oauth":
            return os.path.exists(os.path.join(h, ".gemini", "oauth_creds.json"))
        k = p.get("api_key_file")
        if k and os.path.exists(os.path.expanduser(k)):
            return True
        return os.path.exists(os.path.join(h, ".gemini", "settings.json"))
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
            or not shutil.which("systemd-run") or not shutil.which("systemctl")):
        return False
    clave = (os.getuid(), os.environ.get("XDG_RUNTIME_DIR", ""),
             os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""))
    ahora_mono = time.monotonic()
    guardado = _SYSTEMD_USUARIO_CACHE.get(clave)
    if guardado and ahora_mono - guardado[0] < 15:
        return guardado[1]
    try:
        r = _SUBPROCESS_RUN_ORIGINAL(
            ["systemctl", "--user", "show-environment"],
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
    return (["systemd-run", "--user", "--scope", "--quiet",
             f"--unit={unidad}", "--", *cmd], unidad + ".scope")


def _terminar_aislado(proceso, scope=None, gracia=0.7):
    """Mata el cgroup completo; usa el grupo POSIX como respaldo portable."""
    if scope:
        def matar(senal):
            _SUBPROCESS_RUN_ORIGINAL(
                ["systemctl", "--user", "kill", "--kill-whom=all",
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


def correr(pid, p, prompt, tarea="reasoning", timeout=300, carpeta=None,
           session_id=None, resume=False, solo_lectura=False):
    if not admite_tarea(p, tarea):
        t0 = time.time()
        run_id = f"{pid}-{time.time_ns()}"
        texto = (f"[ERROR capacidad] {pid} ({p.get('provider', '?')}) no admite "
                 f"la tarea '{tarea}'")
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

    prov = p.get("provider")
    if prov == "claude" and not session_id:
        session_id = str(uuid.uuid4())
    env = entorno(pid, p)
    opciones_comando = {"solo_lectura": True} if solo_lectura else {}
    cmd = comando(p, prompt, session_id=session_id, resume=resume,
                  **opciones_comando)
    t0 = time.time()
    f_lock = _lock_proveedor(prov) if prov in SERIALIZAR else None
    proceso = None
    scope = None
    try:
        if f_lock:
            fcntl.flock(f_lock, fcntl.LOCK_EX)
        destino = carpeta if carpeta and os.path.isdir(carpeta) else BASE
        if subprocess.run is not _SUBPROCESS_RUN_ORIGINAL:
            # Punto de inyeccion conservado para consumidores que sustituian
            # el runner (incluida la suite historica) sin arrancar una IA real.
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, env=env, cwd=destino)
            out, err, rc = r.stdout, r.stderr, r.returncode
        else:
            cmd_aislado, scope = _scope_systemd(cmd)
            proceso = subprocess.Popen(cmd_aislado, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True,
                                       env=env, cwd=destino,
                                       start_new_session=True)
            out, err = proceso.communicate(timeout=timeout)
            rc = proceso.returncode
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
TERMINALES = [("kitty", ["kitty", "--title", "{titulo}", "-e", "bash", "-lc", "{cmd}"]),
              ("ptyxis", ["ptyxis", "--title", "{titulo}", "--", "bash", "-lc", "{cmd}"]),
              ("gnome-terminal", ["gnome-terminal", "--title", "{titulo}", "--",
                                  "bash", "-lc", "{cmd}"]),
              ("konsole", ["konsole", "-e", "bash", "-lc", "{cmd}"]),
              ("xterm", ["xterm", "-T", "{titulo}", "-e", "bash", "-lc", "{cmd}"])]


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
    import shutil
    for t, plantilla in TERMINALES:
        if shutil.which(t):
            return t, plantilla
    return None, None


def lanzar_login(pid, p, titulo=None):
    """Abre una terminal con el entorno listo para autenticar esa cuenta."""
    if not id_perfil_valido(pid):
        return False, "id de cuenta invalido"
    if p.get("provider") not in ("claude", "gpt", "antigravity", "gemini", "minimax"):
        return False, "proveedor no permitido"
    if not navegador_valido(p.get("navegador")):
        return False, "navegador no permitido"
    t, plantilla = terminal_disponible()
    if not t:
        return False, "no encontre ninguna terminal grafica instalada"
    cmd_auth = cmd_login(pid, p).split("#")[0].strip()
    titulo = titulo or f"LOGIN · {pid}"
    guion = (
        "clear; "
        f"printf '\\n  \\033[1mCONECTAR {pid}\\033[0m\\n'; "
        f"printf '  \\033[2m{p.get('provider','')} · navegador: {p.get('navegador') or 'por defecto'}\\033[0m\\n\\n'; "
        + (("printf '  Escribe \\033[33m/login\\033[0m y autoriza en el navegador.\\n\\n'; "
            ) if p.get("provider") == "claude" else
           ("printf '  Sigue las instrucciones en pantalla.\\n\\n'; "))
        + cmd_auth + "; "
        "printf '\\n  --- terminado ---\\n'; exec bash"
    )
    args = [x.replace("{titulo}", titulo).replace("{cmd}", guion) for x in plantilla]
    try:
        subprocess.Popen(args, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, f"terminal abierta ({t})"
    except Exception as e:
        return False, str(e)

# ---------------- cuenta activa por proveedor ----------------
ENTORNO_SH = os.path.join(BASE, "state", "entorno.sh")


def activas():
    """{proveedor: id_de_cuenta} que usan por defecto TODAS las terminales."""
    return cfg().get("_activas", {})


def escribir_entorno():
    """Genera el archivo que cada terminal carga al abrirse.

    Con esto, escribir 'claude' o 'codex' en cualquier terminal usa
    automaticamente la cuenta activa, sin autenticar nada de nuevo.
    """
    c = cfg()
    ps = c.get("profiles", {})
    act = c.get("_activas", {})
    lineas = ["# Generado por 'orq usar' — no editar a mano",
              "# Lo carga shell.sh en cada terminal nueva.", ""]
    for prov, pid in sorted(act.items()):
        p = ps.get(pid)
        if not p or not autenticado(pid, p):
            continue
        h = home_de(pid, p)
        if prov == "minimax":
            # exportar ANTHROPIC_BASE_URL aqui secuestraria la cuenta Claude
            # real de todas las terminales. MiniMax se usa con 'minimax' o
            # con 'orquse <id>' dentro de una sola terminal.
            continue
        if prov == "claude":
            lineas += [f'export CLAUDE_CONFIG_DIR="{h}"']
        elif prov == "gpt":
            lineas += [f'export CODEX_HOME="{h}"']
        elif prov == "gemini":
            lineas += [f'export GEMINI_CLI_HOME="{h}"',
                       'export GEMINI_CLI_TRUST_WORKSPACE=true']
            if p.get("auth") == "oauth":
                lineas.append('export GOOGLE_GENAI_USE_GCA=true')
            k = p.get("api_key_file")
            if k and os.path.exists(os.path.expanduser(k)):
                lineas.append(f'export GEMINI_API_KEY="$(cat "{k}" 2>/dev/null)"')
        lineas.append(f'export ORQ_{prov.upper()}_CUENTA="{pid}"')
    lineas.append("")
    lineas.append(f'export ORQ_PERMISOS_TOTALES={"1" if c.get("_permisos_totales", False) else "0"}')
    lineas.append("")
    with bloqueo():
        os.makedirs(os.path.dirname(ENTORNO_SH), exist_ok=True)
        tmp = ENTORNO_SH + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lineas))
        os.replace(tmp, ENTORNO_SH)
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
SCRATCH_AGY = os.path.expanduser("~/.gemini/antigravity-cli/scratch")


def _imagenes_en(d):
    import glob
    r = []
    for ext in ("png", "jpg", "jpeg", "webp"):
        r += glob.glob(os.path.join(d, f"*.{ext}"))
    return {f: os.path.getmtime(f) for f in r}


def extension_real(ruta):
    """Devuelve la extension segun el contenido, no segun el nombre."""
    try:
        with open(ruta, "rb") as f:
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


def generar_imagen(pid, p, prompt, destino=None, timeout=420):
    """Genera una imagen y la deja en 'destino' con la extension correcta."""
    if not admite_tarea(p, "imagen"):
        return {"perfil": pid, "texto": f"[ERROR capacidad] {pid} no genera imagenes",
                "tokens": 0, "seg": 0.0, "rc": 2, "run_id": "", "archivos": [],
                "origen": [], "limitado": None}
    antes = _imagenes_en(SCRATCH_AGY)
    antes_dst = _imagenes_en(destino) if destino and os.path.isdir(destino) else {}
    env = entorno(pid, p)
    cmd = comando(p, prompt)
    t0 = time.time()
    proceso = None
    scope = None
    try:
        if subprocess.run is not _SUBPROCESS_RUN_ORIGINAL:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                               env=env, cwd=destino or BASE)
            out, err, rc = r.stdout, r.stderr, r.returncode
        else:
            cmd_aislado, scope = _scope_systemd(cmd)
            proceso = subprocess.Popen(
                cmd_aislado, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=env, cwd=destino or BASE, start_new_session=True,
            )
            out, err = proceso.communicate(timeout=timeout)
            rc = proceso.returncode
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
    if destino and os.path.isdir(destino):
        ahora_dst = _imagenes_en(destino)
        for f, m in sorted(ahora_dst.items(), key=lambda x: -x[1]):
            if f not in antes_dst or m > antes_dst.get(f, 0):
                ext = extension_real(f)
                if ext and not f.lower().endswith("." + ext):
                    nuevo = os.path.splitext(f)[0] + "." + ext
                    if not os.path.exists(nuevo):
                        os.rename(f, nuevo); f = nuevo
                guardadas.append(f)
    # 2) si no, lo que quedo en el scratch de agy
    nuevas = _imagenes_en(SCRATCH_AGY)
    creadas = sorted([f for f, m in nuevas.items()
                      if f not in antes or m > antes.get(f, 0)],
                     key=lambda f: nuevas[f], reverse=True)
    if creadas and destino and not guardadas:
        os.makedirs(destino, exist_ok=True)
        for i, f in enumerate(creadas[:4]):
            ext = extension_real(f) or "png"
            base = os.path.splitext(os.path.basename(f))[0]
            dst = os.path.join(destino, f"{base}{'' if i == 0 else '-' + str(i)}.{ext}")
            n = 1
            while os.path.exists(dst):
                dst = os.path.join(destino, f"{base}-{n}.{ext}"); n += 1
            shutil.copy2(f, dst)
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

# ---------------- sesiones externas y serializacion ----------------
# Los CLIs de suscripcion no toleran bien varias sesiones a la vez: si tienes
# un 'codex --yolo' trabajando en otra terminal, una llamada nuestra se encola.
PATRON_PROC = {"claude": r"(^|/)claude(\s|$)", "gpt": r"(^|/)codex(\s|$)",
               "antigravity": r"(^|/)agy(\s|$)"}
SERIALIZAR = {"gpt"}          # proveedores que exigen una llamada a la vez


def sesiones_externas():
    """Procesos de CLI vivos que NO lanzo el orquestador (tus terminales)."""
    out = {}
    try:
        ps = subprocess.run(["ps", "-eo", "pid,etimes,args"],
                            capture_output=True, text=True, timeout=8).stdout
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
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    return open(ruta, "a+")


@contextlib.contextmanager
def bloqueo_proyecto(carpeta):
    """Un solo proyecto escritor por carpeta, incluso desde otras terminales."""
    real = os.path.realpath(os.path.abspath(carpeta))
    try:
        repo = _SUBPROCESS_RUN_ORIGINAL(
            ["git", "-C", real, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if repo.returncode == 0 and repo.stdout.strip():
            real = os.path.realpath(repo.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    clave = hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()
    directorio = os.path.join(BASE, "state", "project-locks")
    os.makedirs(directorio, exist_ok=True)
    archivo = open(os.path.join(directorio, clave + ".lock"), "a+")
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
    os.makedirs(carpeta, exist_ok=True)
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

    permitido = False
    if base == "git":
        permitido = (
            args in (["diff", "--check"],
                     ["diff", "--cached", "--check"],
                     ["diff", "--check", "--cached"])
            or (args[:1] == ["status"] and all(
                x in {"--porcelain", "--short", "--branch", "-sb"}
                for x in args[1:]))
            or args == ["rev-parse", "--is-inside-work-tree"]
            or args[:1] == ["ls-files"]
        )
    elif base in {"python", "python3"}:
        permitido = len(args) >= 2 and args[0] == "-m" and args[1] in {
            "unittest", "pytest", "compileall", "py_compile", "ruff", "mypy"
        }
    elif base in {"pytest", "py.test", "ruff", "mypy", "eslint"}:
        permitido = True
    elif base == "node":
        permitido = bool(args) and args[0] == "--check"
    elif base in {"bash", "sh"}:
        permitido = bool(args) and args[0] == "-n"
    elif base in {"npm", "pnpm", "yarn"}:
        permitido = args[:1] == ["test"] or (
            len(args) >= 2 and args[0] == "run"
            and args[1] in {"test", "lint", "check", "build", "typecheck"}
        )
    elif base == "cargo":
        permitido = bool(args) and args[0] in {"test", "check", "clippy", "fmt"}
    elif base == "go":
        permitido = bool(args) and args[0] in {"test", "vet"}
    elif base == "make":
        permitido = bool(args) and all(
            not x.startswith("-") and x in {"test", "check", "lint", "build"}
            for x in args
        )
    elif base == "tsc":
        permitido = not args or "--noEmit" in args
    elif base == "systemd-analyze":
        permitido = "verify" in args and "security" not in args
    return argv if permitido else None


def ejecutar_comando_verificacion(comando, carpeta, timeout=600):
    """Ejecuta una comprobacion permitida y devuelve evidencia propia."""
    argv = _argv_verificacion(comando)
    if argv is None:
        return {"comando": str(comando)[:160], "rc": 126,
                "resultado": "comando no permitido por la politica de verificacion"}
    if not shutil.which(argv[0]):
        return {"comando": comando, "rc": 127,
                "resultado": "binario de verificacion no disponible"}
    limite = max(5, min(900, int(timeout or 600)))
    proceso = None
    scope = None
    try:
        aislado, scope = _scope_systemd(argv)
        proceso = subprocess.Popen(
            aislado, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=carpeta, start_new_session=True,
        )
        out, err = proceso.communicate(timeout=limite)
        rc = proceso.returncode
    except subprocess.TimeoutExpired as e:
        if proceso is not None:
            _terminar_aislado(proceso, scope)
            recogida = _salida_tras_interrupcion(proceso)
            out, err = recogida if recogida else (e.stdout or "", e.stderr or "")
        else:
            out, err = e.stdout or "", e.stderr or ""
        rc = 124
    except (OSError, subprocess.SubprocessError) as e:
        out, err, rc = "", str(e), 127
    salida = ((out or "") + ("\n" if out and err else "") + (err or "")).strip()
    if rc == 124:
        salida = (salida + f"\ntimeout tras {limite}s").strip()
    return {"comando": " ".join(shlex.quote(x) for x in argv), "rc": int(rc),
            "resultado": salida[-1600:] or ("sin salida" if rc == 0 else "fallo sin salida")}


def verificar_proyecto(plan, carpeta, resultados, timeout=600, preferir=None):
    """Verificacion final independiente, con pruebas reales y salida estructurada."""
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
        ultimo = correr(candidato["pid"], candidato["p"], prompt, "review",
                        timeout, carpeta=carpeta, solo_lectura=True)
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
        comprobaciones_reales = [
            ejecutar_comando_verificacion(x["comando"], carpeta, timeout)
            for x in comprobaciones
        ]
        hallazgos_reales = list(hallazgos)
        for prueba in comprobaciones_reales:
            if prueba["rc"] != 0:
                hallazgos_reales.append(
                    f"fallo real rc={prueba['rc']}: {prueba['comando']}"
                )
        ultimo["verificacion_ok"] = bool(
            dato["ok"] and all(x["rc"] == 0 for x in comprobaciones_reales)
            and not hallazgos_reales
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


def _git(carpeta, *args, timeout=120):
    """Git sin shell; nunca incorpora stdout de error a mensajes publicos."""
    try:
        return _SUBPROCESS_RUN_ORIGINAL(
            ["git", *args], cwd=carpeta, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def publicar_repo(carpeta, mensaje="chore: cambios verificados por Orquesta IA"):
    """Escanea, confirma y publica un repo sin permitir credenciales.

    Se revisan el arbol completo, los blobs staged y cualquier commit local que
    aun no exista en el remoto. Nunca se hace force-push ni merge automatico.
    """
    carpeta = os.path.realpath(os.path.abspath(os.path.expanduser(carpeta)))
    raiz_r = _git(carpeta, "rev-parse", "--show-toplevel")
    if not raiz_r or raiz_r.returncode != 0:
        return {"ok": False, "fase": "git", "detalle": "la carpeta no es un repositorio Git"}
    raiz = os.path.realpath(raiz_r.stdout.strip())
    rama_r = _git(raiz, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not rama_r or rama_r.returncode != 0 or not rama_r.stdout.strip():
        return {"ok": False, "fase": "git", "detalle": "HEAD esta separado de una rama"}
    rama = rama_r.stdout.strip()
    remoto_r = _git(raiz, "remote", "get-url", "origin")
    if not remoto_r or remoto_r.returncode != 0:
        return {"ok": False, "fase": "git", "detalle": "falta el remoto origin"}
    remoto = remoto_r.stdout.strip()
    if re.match(r"^[a-z][a-z0-9+.-]*://[^/@\s]+@", remoto, re.I):
        return {"ok": False, "fase": "seguridad",
                "detalle": "origin contiene credenciales; usa SSH o un credential helper"}

    fetch = _git(raiz, "fetch", "--quiet", "origin", timeout=180)
    if not fetch or fetch.returncode != 0:
        return {"ok": False, "fase": "fetch", "detalle": "no pude actualizar origin"}
    ref_remota = f"refs/remotes/origin/{rama}"
    existe = _git(raiz, "show-ref", "--verify", "--quiet", ref_remota)
    commits_locales = []
    if existe and existe.returncode == 0:
        cuenta = _git(raiz, "rev-list", "--left-right", "--count",
                      f"HEAD...origin/{rama}")
        if not cuenta or cuenta.returncode != 0:
            return {"ok": False, "fase": "git", "detalle": "no pude comparar con origin"}
        try:
            delante, detras = (int(x) for x in cuenta.stdout.split())
        except (ValueError, TypeError):
            return {"ok": False, "fase": "git", "detalle": "comparacion remota invalida"}
        if detras:
            return {"ok": False, "fase": "sincronizacion",
                    "detalle": "origin tiene cambios nuevos; integra antes de publicar"}
        if delante:
            lista = _git(raiz, "rev-list", f"origin/{rama}..HEAD")
            if not lista or lista.returncode != 0:
                return {"ok": False, "fase": "git", "detalle": "no pude auditar commits locales"}
            commits_locales = [x for x in lista.stdout.splitlines() if x]

    scanner = os.path.join(BASE, "tools", "scan-secretos.sh")
    if not os.path.isfile(scanner):
        return {"ok": False, "fase": "seguridad", "detalle": "falta el escaner de secretos"}

    def escanear(*opciones):
        try:
            return _SUBPROCESS_RUN_ORIGINAL(
                ["bash", scanner, *opciones, "--repo", raiz],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                timeout=180, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    for commit in commits_locales:
        revisado = escanear("--commit", commit)
        if not revisado or revisado.returncode != 0:
            return {"ok": False, "fase": "seguridad",
                    "detalle": "un commit local no supero el escaneo de secretos"}
    completo = escanear("--todo")
    if not completo or completo.returncode != 0:
        return {"ok": False, "fase": "seguridad",
                "detalle": "el arbol de trabajo no supero el escaneo de secretos"}

    add = _git(raiz, "add", "-A")
    if not add or add.returncode != 0:
        return {"ok": False, "fase": "stage", "detalle": "git add fallo"}
    staged = escanear("--staged")
    if not staged or staged.returncode != 0:
        return {"ok": False, "fase": "seguridad",
                "detalle": "el indice no supero el escaneo de secretos"}

    hay_stage = _git(raiz, "diff", "--cached", "--quiet")
    creado = False
    if hay_stage is None:
        return {"ok": False, "fase": "git", "detalle": "no pude leer el indice"}
    if hay_stage.returncode == 1:
        commit = _git(raiz, "commit", "-m", str(mensaje)[:200], timeout=180)
        if not commit or commit.returncode != 0:
            return {"ok": False, "fase": "commit", "detalle": "git commit fallo"}
        creado = True
        ultimo = _git(raiz, "rev-parse", "HEAD")
        if not ultimo or ultimo.returncode != 0:
            return {"ok": False, "fase": "git", "detalle": "no pude verificar el commit"}
        revisado = escanear("--commit", ultimo.stdout.strip())
        if not revisado or revisado.returncode != 0:
            return {"ok": False, "fase": "seguridad",
                    "detalle": "el commit nuevo no supero el escaneo"}
    elif hay_stage.returncode != 0:
        return {"ok": False, "fase": "git", "detalle": "estado del indice invalido"}

    push_args = ["push"]
    if not existe or existe.returncode != 0:
        push_args += ["--set-upstream", "origin", rama]
    else:
        push_args += ["origin", rama]
    push = _git(raiz, *push_args, timeout=300)
    if not push or push.returncode != 0:
        return {"ok": False, "fase": "push",
                "detalle": "el commit es local y seguro, pero git push fallo"}
    head = _git(raiz, "rev-parse", "--short", "HEAD")
    return {"ok": True, "fase": "listo", "detalle": "publicado sin secretos",
            "commit": head.stdout.strip() if head and head.returncode == 0 else "",
            "creado": creado, "raiz": raiz}

# ---------------- USO REAL (todas las sesiones, no solo las de Orquesta) ----------------
# El ledger de Orquesta solo ve lo que Orquesta gasta. Pero tus sesiones
# manuales (claude, codex --yolo, agy) consumen de la MISMA cuota. Aqui se
# leen los registros que cada CLI deja en disco para ver el consumo real.
CACHE_REAL = os.path.join(BASE, "state", "uso_real.json")
USO_CACHE_VERSION = 2


def _dirs_sesiones(pid, p):
    prov = p.get("provider")
    h = home_de(pid, p)
    if prov == "claude":
        return [os.path.join(h, "projects")]
    if prov == "gpt":
        return [os.path.join(h, "sessions")]
    if prov == "antigravity":
        return [os.path.join(h, "conversations")]
    return []


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


def _uso_archivo_claude(f):
    """Suma el uso de una sesion de Claude Code. Devuelve (por_dia, por_hora)."""
    dias, horas = {}, {}
    try:
        with open(f, errors="ignore") as fh:
            for linea in fh:
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
    except OSError:
        pass
    return dias, horas


def _uso_archivo_codex(f):
    dias, horas = {}, {}
    ult = 0
    ts_ult = ""
    uso_ult = {}
    try:
        with open(f, errors="ignore") as fh:
            for linea in fh:
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
    except OSError:
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
        if not os.path.isdir(d):
            continue
        for raiz, _, files in os.walk(d):
            for f in files:
                if f.endswith(".jsonl"):
                    ruta = os.path.join(raiz, f)
                    try:
                        archivos[ruta] = os.path.getmtime(ruta)
                    except OSError:
                        pass
    if (not refrescar and entrada.get("version") == USO_CACHE_VERSION
            and entrada.get("archivos") == archivos):
        return {"dias": entrada.get("dias", {}), "horas": entrada.get("horas", {}),
                "archivos": len(archivos)}

    # Si un JSONL crece se relee el conjunto completo. Sumar el archivo nuevo
    # sobre su agregado anterior duplicaba todos los eventos ya contabilizados.
    dias, horas = {}, {}
    for ruta, m in archivos.items():
        d1, h1 = (_uso_archivo_claude(ruta) if prov == "claude"
                  else _uso_archivo_codex(ruta) if prov == "gpt" else ({}, {}))
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
    d = os.path.join(home_de(pid, p), "sessions")
    if not os.path.isdir(d):
        return None
    archivos = []
    for raiz, _, fs in os.walk(d):
        for f in fs:
            if f.startswith("rollout-") and f.endswith(".jsonl"):
                ruta = os.path.join(raiz, f)
                try:
                    archivos.append((os.path.getmtime(ruta), ruta))
                except OSError:
                    pass
    archivos.sort(reverse=True)
    for _, ruta in archivos[:6]:
        ult = None
        try:
            with open(ruta, errors="ignore") as fh:
                for linea in fh:
                    if "rate_limits" not in linea:
                        continue
                    try:
                        d2 = json.loads(linea)
                    except json.JSONDecodeError:
                        continue
                    rl = (d2.get("payload") or {}).get("rate_limits")
                    if rl:
                        ult = (d2.get("timestamp"), rl)
        except OSError:
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
    for ruta in (os.path.join(home_de(pid, p), ".claude.json"),
                 os.path.expanduser("~/.claude.json") if home_de(pid, p).endswith(".claude") else None):
        if not ruta or not os.path.exists(ruta):
            continue
        try:
            with open(ruta) as archivo:
                d = json.load(archivo)
        except Exception:
            continue
        oa = d.get("oauthAccount") or {}
        t = oa.get("organizationRateLimitTier") or oa.get("userRateLimitTier")
        if t:
            nom, mult = TIER_CLAUDE.get(t, (t, 1))
            return {"tier": t, "nombre": nom, "multiplicador": mult,
                    "correo": oa.get("emailAddress")}
    try:
        with open(os.path.join(home_de(pid, p), ".credentials.json")) as archivo:
            cr = json.load(archivo)
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
    home = os.path.expanduser("~")
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
