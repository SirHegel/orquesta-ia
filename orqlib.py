"""Nucleo compartido del orquestador multi-cuenta.

Estado en disco con bloqueo fcntl: cualquier numero de terminales puede usar
el sistema a la vez sin corromper el ledger ni los contadores.
"""
import json, os, re, shutil, subprocess, sys, threading, time, datetime, fcntl, contextlib
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

BASE = os.environ.get("ORQ_HOME") or os.path.dirname(os.path.abspath(__file__))
ACCOUNTS = os.path.join(BASE, "accounts")
PROFILES = os.path.join(BASE, "profiles.json")
LEDGER = os.path.join(BASE, "state", "ledger.jsonl")
LIMITS = os.path.join(BASE, "state", "limits.json")
SCORES = os.path.join(BASE, "state", "scores.json")
LOCK = os.path.join(BASE, "state", ".lock")

TAREAS = ["code", "agentic", "reasoning", "review", "writing",
          "research", "edicion", "imagen", "bulk"]

# Solo estos proveedores saben generar imagenes.
PROVEEDORES_IMAGEN = {"antigravity"}

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
        return os.path.expanduser(h)
    return os.path.join(ACCOUNTS, pid)


def entorno(pid, p):
    env = dict(os.environ)
    prov = p.get("provider")
    h = home_de(pid, p)
    if prov == "claude":
        env["CLAUDE_CONFIG_DIR"] = h
    elif prov == "gpt":
        env["CODEX_HOME"] = h
    elif prov == "antigravity":
        pass
    elif prov == "gemini":
        env["GEMINI_CLI_HOME"] = h
        env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
        k = p.get("api_key_file")
        if p.get("auth") == "oauth":
            env["GOOGLE_GENAI_USE_GCA"] = "true"
        elif k and os.path.exists(os.path.expanduser(k)):
            env["GEMINI_API_KEY"] = open(os.path.expanduser(k)).read().strip()
    for k, v in (p.get("env") or {}).items():
        env[k] = v
    return env


# Maxima potencia: modelo y esfuerzo mas altos de cada proveedor.
POTENCIA_MAX = {
    "claude": {"model": "claude-opus-5", "effort": "xhigh"},
    "gpt": {"reasoning": "high"},
    "antigravity": {"model": "gemini-3.1-pro-high", "effort": "high"},
    "gemini": {},
}


def potencia_maxima():
    return cfg().get("_potencia_maxima", True)


PERMISOS = {
    "claude": ["--dangerously-skip-permissions"],
    "gpt": ["--dangerously-bypass-approvals-and-sandbox"],
    "antigravity": ["--dangerously-skip-permissions"],
    "gemini": ["--yolo"],
}


def permisos_activos():
    return cfg().get("_permisos_totales", True)


def comando(p, prompt):
    prov = p.get("provider")
    perm = PERMISOS.get(prov, []) if permisos_activos() else []
    mx = POTENCIA_MAX.get(prov, {}) if potencia_maxima() else {}
    # el perfil manda sobre el ajuste global
    modelo = p.get("model") or mx.get("model")
    if prov == "claude":
        base = ["claude", "-p", "--output-format", "json"] + perm
        if modelo:
            base += ["--model", modelo]
        if mx.get("effort"):
            base += ["--effort", mx["effort"]]
        return base + [prompt]
    if prov == "gpt":
        base = ["codex", "exec", "--skip-git-repo-check"] + perm
        if modelo:
            base += ["-m", modelo]
        if mx.get("reasoning"):
            base += ["-c", f'model_reasoning_effort="{mx["reasoning"]}"']
        return base + [prompt]
    if prov == "antigravity":
        base = ["agy", "--output-format", "json"] + perm
        if p.get("model"):
            base += ["--model", p["model"]]
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
    pre = f'BROWSER={nav} ' if nav else ""
    if prov == "claude":
        return f'{pre}CLAUDE_CONFIG_DIR="{h}" claude   # dentro escribe: /login'
    if prov == "gpt":
        return f'{pre}CODEX_HOME="{h}" codex login'
    if prov == "antigravity":
        import shutil
        return shutil.which("agy") is not None
    if prov == "antigravity":
        return "agy   # si pide sesion, autoriza en el navegador"
    if prov == "gemini":
        if p.get("auth") == "oauth":
            return (f'GEMINI_CLI_HOME="{h}" GOOGLE_GENAI_USE_GCA=true '
                    f'BROWSER={nav or "firefox"} gemini   # autoriza con la cuenta de Google')
        return (f'mkdir -p "{h}" && printf %s "TU_API_KEY" > "{h}/api_key" '
                f'&& chmod 600 "{h}/api_key"')
    return "proveedor desconocido"


# ---------------- limites de uso ----------------
PAT_LIMITE = re.compile(
    r"(rate.?limit|usage limit|limit reached|too many requests|quota|429|"
    r"limite de uso|has alcanzado)", re.I)
PAT_RESET = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)


def detectar_limite(pid, p, texto):
    """Si la salida indica limite de uso, registra hasta cuando esta bloqueado."""
    if not texto or not PAT_LIMITE.search(texto):
        return None
    horas = p.get("ventana_horas") or VENTANA_PLAN.get(p.get("plan", "desconocido"), 5)
    hasta = ahora() + datetime.timedelta(hours=horas)
    m = PAT_RESET.search(texto)
    if m:
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
        L[pid] = {"bloqueado_hasta": hasta.isoformat(timespec="seconds"),
                  "detectado": ahora().isoformat(timespec="seconds"),
                  "motivo": texto.strip()[:200]}
        _escribir(LIMITS, L)
    return hasta


def bloqueado(pid):
    L = limites().get(pid)
    if not L:
        return None
    try:
        hasta = datetime.datetime.fromisoformat(L["bloqueado_hasta"])
    except Exception:
        return None
    return hasta if hasta > ahora() else None


def limpiar_limite(pid):
    with bloqueo():
        L = limites()
        L.pop(pid, None)
        _escribir(LIMITS, L)


# ---------------- extraccion ----------------
def extraer(provider, stdout, stderr=""):
    if provider == "claude":
        try:
            d = json.loads(stdout)
            u = d.get("usage", {}) or {}
            tok = (u.get("input_tokens", 0) + u.get("output_tokens", 0)
                   + u.get("cache_read_input_tokens", 0)
                   + u.get("cache_creation_input_tokens", 0))
            return (d.get("result") or ""), tok
        except Exception:
            return (stdout or "").strip(), 0
    if provider == "antigravity":
        try:
            d = json.loads(stdout)
            u = d.get("usage", {}) or {}
            return (d.get("response") or "").strip(), u.get("total_tokens", 0)
        except Exception:
            return (stdout or "").strip(), 0
    if provider == "gpt":
        m = re.findall(r"tokens used\s*\n\s*([\d.,\s]*\d)", stderr or "")
        tok = sum(int(re.sub(r"\D", "", x)) for x in m if re.sub(r"\D", "", x))
        return (stdout or "").strip(), tok
    return (stdout or "").strip(), 0


# ---------------- ejecucion ----------------
def correr(pid, p, prompt, tarea="reasoning", timeout=300, carpeta=None):
    env = entorno(pid, p)
    cmd = comando(p, prompt)
    prov = p.get("provider")
    t0 = time.time()
    f_lock = _lock_proveedor(prov) if prov in SERIALIZAR else None
    try:
        if f_lock:
            fcntl.flock(f_lock, fcntl.LOCK_EX)
        destino = carpeta if carpeta and os.path.isdir(carpeta) else BASE
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env, cwd=destino)
        out, err, rc = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        out, err, rc = "", f"timeout tras {timeout}s", 124
    except FileNotFoundError as e:
        out, err, rc = "", f"binario no encontrado: {e}", 127
    finally:
        if f_lock:
            try:
                fcntl.flock(f_lock, fcntl.LOCK_UN)
            finally:
                f_lock.close()
    dur = round(time.time() - t0, 1)
    texto, tok = extraer(p["provider"], out, err)
    lim = detectar_limite(pid, p, (texto or "") + "\n" + (err or ""))
    if rc != 0 and not texto:
        texto = f"[ERROR rc={rc}] {(err or '').strip()[:400]}"
    run_id = f"{pid}-{int(t0)}"
    log({"ts": ahora().isoformat(timespec="seconds"), "fecha": hoy(),
         "semana": ahora().strftime("%G-S%V"), "mes": ahora().strftime("%Y-%m"),
         "perfil": pid, "provider": p["provider"], "tarea": tarea,
         "tokens": tok, "seg": dur, "rc": rc, "limite": bool(lim),
         "sesion": os.environ.get("ORQ_SESION", "sin-sesion"),
         "term": os.environ.get("ORQ_SESION_TERM", ""),
         "carpeta": carpeta or "", "prompt": prompt[:200], "run_id": run_id})
    return {"perfil": pid, "label": p.get("label", pid), "texto": texto,
            "tokens": tok, "seg": dur, "rc": rc, "run_id": run_id,
            "limitado": lim.isoformat(timespec="seconds") if lim else None}


# ---------------- routing ----------------
def disponibles(incluir_bloqueados=False):
    out = {}
    for pid, p in cfg().get("profiles", {}).items():
        if not p.get("enabled", True):
            continue
        if not autenticado(pid, p):
            continue
        if not incluir_bloqueados and bloqueado(pid):
            continue
        out[pid] = p
    return out


def puntuar(pid, p, tarea):
    if tarea == "imagen" and p.get("provider") not in PROVEEDORES_IMAGEN:
        return 0.0, "no genera imagenes"
    base = (p.get("weights") or {}).get(tarea, 5)
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
    if pct is None:
        g = cuota_global(pid, p)
        if g.get("fuente") == "declarado" and g.get("pct") is not None:
            pct = g["pct"]
            q = dict(q); q["fuente"] = "proveedor"      # se trata como dato firme
            q["ventana_min"] = (p.get("ventana_horas") or 5) * 60
    if pct is not None and q.get("fuente") == "proveedor":
        if pct >= 97:
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
        if total > 0:
            parte = gastos.get(pid, 0) / total          # 0 = sin usar, 1 = se lo lleva todo
            justo = 1.0 / len(hermanas)
            # quien va por debajo de su parte justa sube; quien va por encima baja
            factor *= max(0.45, min(1.55, 1 + (justo - parte)))
            notas.append(f"reparto {parte*100:.0f}% de {p.get('provider')}")
    if not notas:
        notas.append("sin tope")
    return base * mult * factor, " · ".join(notas)


def ranking(tarea, proposito=None, incluir_bloqueados=False):
    disp = disponibles(incluir_bloqueados)
    if proposito:
        f = {k: v for k, v in disp.items()
             if v.get("proposito") in (proposito, "general")}
        disp = f or disp
    out = []
    for pid, p in disp.items():
        pts, nota = puntuar(pid, p, tarea)
        out.append({"pid": pid, "p": p, "pts": pts, "nota": nota})
    out.sort(key=lambda x: -x["pts"])
    return [x for x in out if x["pts"] > 0]

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


def terminal_disponible():
    import shutil
    for t, plantilla in TERMINALES:
        if shutil.which(t):
            return t, plantilla
    return None, None


def lanzar_login(pid, p, titulo=None):
    """Abre una terminal con el entorno listo para autenticar esa cuenta."""
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
    lineas.append(f'export ORQ_PERMISOS_TOTALES={"1" if c.get("_permisos_totales", True) else "0"}')
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
    antes = _imagenes_en(SCRATCH_AGY)
    antes_dst = _imagenes_en(destino) if destino and os.path.isdir(destino) else {}
    env = entorno(pid, p)
    cmd = ["agy", "--dangerously-skip-permissions", "--output-format", "json",
           "-p", prompt]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=env, cwd=destino or BASE)
        out, err, rc = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        out, err, rc = "", f"timeout tras {timeout}s", 124
    dur = round(time.time() - t0, 1)
    texto, tok = extraer("antigravity", out, err)

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

    run_id = f"{pid}-{int(t0)}"
    log({"ts": ahora().isoformat(timespec="seconds"), "fecha": hoy(),
         "semana": ahora().strftime("%G-S%V"), "mes": ahora().strftime("%Y-%m"),
         "perfil": pid, "provider": p["provider"], "tarea": "imagen",
         "tokens": tok, "seg": dur, "rc": rc, "limite": False,
         "sesion": os.environ.get("ORQ_SESION", "sin-sesion"),
         "term": os.environ.get("ORQ_SESION_TERM", ""),
         "prompt": prompt[:200], "run_id": run_id})
    return {"perfil": pid, "texto": texto, "tokens": tok, "seg": dur, "rc": rc,
            "run_id": run_id, "archivos": guardadas, "origen": creadas[:4]}

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
    """Si el plan salio como cadena lineal, lo reestructura para que haya paralelo."""
    tareas = plan.get("tareas") or []
    if len(tareas) < 3:
        return plan, None
    olas = _ordenar_por_olas(tareas)
    if max((len(o) for o in olas), default=0) > 1:
        return plan, None                     # ya tiene paralelo
    # cadena lineal: la primera queda de cimientos y el resto cuelga de ella
    base = tareas[0]["id"]
    for t in tareas[1:]:
        t["depende"] = [base]
    # la ultima de tipo review vuelve a depender de todas (cierre)
    for t in reversed(tareas[1:]):
        if t.get("tipo") == "review":
            t["depende"] = [x["id"] for x in tareas if x["id"] != t["id"]]
            break
    return plan, (f"el plan venia en cadena lineal; reestructurado a "
                  f"{len(_ordenar_por_olas(tareas))} olas para repartirlo")


def _mejor_para(tarea):
    r = ranking(tarea)
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


def planificar(descripcion, carpeta, timeout=300):
    """Fase 0: la cuenta mas fuerte en 'agentic' propone el plan."""
    pid, p = _mejor_para("agentic")
    if not pid:
        return None, "no hay ninguna cuenta disponible para planificar"
    prompt = (f"Eres el arquitecto de un proyecto que se construira en la carpeta "
              f"{carpeta}. El encargo es:\n\n{descripcion}\n\n{ESQUEMA_PLAN}")
    r = correr(pid, p, prompt, "agentic", timeout)
    plan = _json_de(r["texto"])
    if not plan or not plan.get("tareas"):
        return None, f"{pid} no devolvio un plan valido: {(r['texto'] or '')[:180]}"
    plan["_planificador"] = pid
    plan["_tokens_plan"] = r["tokens"]
    plan, aviso = _reparar_plan(plan, len(disponibles()))
    plan["_aviso"] = aviso
    return plan, None


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


def deliberar(plan, encargo, timeout=240):
    """Las IA opinan sobre quien debe hacer que, antes de gastar en construir.

    Si coinciden en que conviene hacerlo con una sola cuenta, se hace asi.
    """
    disp = disponibles()
    if len(disp) < 2:
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
    votantes = list(disp.items())[:3]
    with ThreadPoolExecutor(max_workers=len(votantes)) as ex:
        votos = list(ex.map(lambda kv: (kv[0], correr(kv[0], kv[1], prompt,
                                                      "reasoning", timeout)),
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
        rk = ranking("agentic")
        elegida = rk[0]["pid"] if rk else None
        return ({t["id"]: elegida for t in plan["tareas"]},
                f"{solos} de {len(props)} coinciden en hacerlo con una sola cuenta ({elegida})")
    # consenso por mayoria tarea a tarea
    final, validas = {}, set(disp)
    for t in plan["tareas"]:
        conteo = {}
        for a in props:
            v = a.get(t["id"])
            if v in validas:
                conteo[v] = conteo.get(v, 0) + 1
        if conteo:
            final[t["id"]] = max(conteo.items(), key=lambda x: x[1])[0]
    return (final or None), (f"consenso de {len(props)} modelos sobre "
                             f"{len(final)} tareas" if final else "sin consenso")


def ejecutar_proyecto(plan, carpeta, timeout=600, callback=None,
                      asignacion=None, contexto_terminal=""):
    """Ejecucion progresiva: cada tarea arranca en cuanto sus dependencias
    terminan, no cuando termina la 'ola' entera. Todas ven el estado vivo."""
    os.makedirs(carpeta, exist_ok=True)
    pz = Pizarra(carpeta, plan, contexto_terminal)
    pendientes = {t["id"]: t for t in plan["tareas"]}
    hechas, resultados = set(), []
    ocupadas = set()
    futuros = {}

    def preparar(t):
        tipo = t.get("tipo", "code")
        if tipo not in TAREAS:
            tipo = "code"
        pid = (asignacion or {}).get(t["id"])
        p = cfg().get("profiles", {}).get(pid) if pid else None
        if not pid or not p or not autenticado(pid, p) or bloqueado(pid):
            r = ranking(tipo)
            libre = next((x for x in r if x["pid"] not in ocupadas), r[0] if r else None)
            if not libre:
                return None, None, tipo
            pid, p = libre["pid"], libre["p"]
        return pid, p, tipo

    def instruccion(t, pid):
        estado = pz.foto()
        arch = pz.archivos_reales()
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
              f"Calidad de produccion, no un esqueleto. "
              f"Al terminar responde en UNA linea: que archivos creaste o cambiaste.")

    with ThreadPoolExecutor(max_workers=max(2, len(disponibles()))) as ex:
        while pendientes or futuros:
            # lanzar todo lo que ya se pueda
            listas = [t for t in pendientes.values()
                      if all(d in hechas for d in (t.get("depende") or []))]
            if not listas and not futuros:          # dependencia rota: desbloquea
                listas = list(pendientes.values())
            for t in listas:
                pid, p, tipo = preparar(t)
                if not pid:
                    continue
                pendientes.pop(t["id"], None)
                ocupadas.add(pid)
                pz.empezar(t, pid)
                if callback:
                    callback("empieza", {"titulo": t["titulo"], "perfil": pid, "tipo": tipo})
                fut = ex.submit(correr, pid, p, instruccion(t, pid), tipo, timeout, carpeta)
                futuros[fut] = (t, pid)
            if not futuros:
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
                hechas.add(t["id"])
                pz.terminar(t, pid, r.get("texto"))
                res = {"id": t["id"], "titulo": t["titulo"], "perfil": pid,
                       "tipo": t.get("tipo"), "rc": r["rc"], "tokens": r["tokens"],
                       "seg": r["seg"], "texto": (r["texto"] or "")[:400],
                       "run_id": r.get("run_id")}
                resultados.append(res)
                if callback:
                    callback("termina", res)
    return resultados


def integrar_proyecto(plan, carpeta, resultados, timeout=600):
    """Fase final: alguien revisa la carpeta entera y la deja conectada."""
    pid, p = _mejor_para("review")
    if not pid:
        return None
    hecho = "\n".join(f"- [{r['perfil']}] {r['titulo']}: {r['texto'][:120]}"
                       for r in resultados)
    prompt = (
        f"Revisa la carpeta {carpeta} del proyecto '{plan.get('nombre')}'.\n"
        f"Lo que hizo cada IA:\n{hecho}\n\n"
        f"Tu trabajo: recorre los archivos reales de la carpeta y dejala COHERENTE. "
        f"Arregla importaciones o rutas que no cuadren entre piezas hechas por "
        f"distintos autores, elimina duplicados y archivos sueltos que no encajen, "
        f"y escribe o corrige el README.md con que es, como se instala y como se usa. "
        f"No reescribas lo que ya funciona. Responde en 5 lineas: que arreglaste.")
    return correr(pid, p, prompt, "review", timeout)

# ---------------- USO REAL (todas las sesiones, no solo las de Orquesta) ----------------
# El ledger de Orquesta solo ve lo que Orquesta gasta. Pero tus sesiones
# manuales (claude, codex --yolo, agy) consumen de la MISMA cuota. Aqui se
# leen los registros que cada CLI deja en disco para ver el consumo real.
CACHE_REAL = os.path.join(BASE, "state", "uso_real.json")


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
                ts = d.get("timestamp") or ""
                dia, hora = ts[:10], ts[:13]
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
                    ts_ult = d.get("timestamp") or pl.get("timestamp") or ts_ult
    except OSError:
        pass
    if ult and ts_ult:
        dia, hora = ts_ult[:10], ts_ult[:13]
        for k, dic in ((dia, dias), (hora, horas)):
            dic[k] = {"entrada": 0, "salida": 0, "cache": 0, "msgs": 1, "total": ult}
    return dias, horas


def uso_real(pid, p, refrescar=False):
    """Consumo real de esa cuenta leyendo los registros del propio CLI."""
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
    dias, horas = {}, {}
    vistos = entrada.get("archivos", {})
    parcial = entrada.get("dias", {}), entrada.get("horas", {})
    nuevos = {}
    for ruta, m in archivos.items():
        if not refrescar and vistos.get(ruta) == m and ruta in entrada.get("hechos", []):
            continue
        d1, h1 = (_uso_archivo_claude(ruta) if prov == "claude"
                  else _uso_archivo_codex(ruta) if prov == "gpt" else ({}, {}))
        for src, dst in ((d1, dias), (h1, horas)):
            for k, v in src.items():
                a = dst.setdefault(k, {"entrada": 0, "salida": 0, "cache": 0, "msgs": 0})
                for kk in ("entrada", "salida", "cache", "msgs"):
                    a[kk] += v.get(kk, 0)
        nuevos[ruta] = m
    # combina con lo cacheado
    for src, dst in ((parcial[0], dias), (parcial[1], horas)):
        for k, v in (src or {}).items():
            a = dst.setdefault(k, {"entrada": 0, "salida": 0, "cache": 0, "msgs": 0})
            for kk in ("entrada", "salida", "cache", "msgs"):
                a[kk] += v.get(kk, 0)
    with bloqueo():
        c = _leer(CACHE_REAL, {})
        c[pid] = {"dias": dias, "horas": horas,
                  "archivos": {**vistos, **nuevos},
                  "hechos": list({**vistos, **nuevos}.keys()),
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
            d = json.load(open(ruta))
        except Exception:
            continue
        oa = d.get("oauthAccount") or {}
        t = oa.get("organizationRateLimitTier") or oa.get("userRateLimitTier")
        if t:
            nom, mult = TIER_CLAUDE.get(t, (t, 1))
            return {"tier": t, "nombre": nom, "multiplicador": mult,
                    "correo": oa.get("emailAddress")}
    try:
        cr = json.load(open(os.path.join(home_de(pid, p), ".credentials.json")))
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

    def foto(self):
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
            if self.en_curso:
                partes.append("EN CURSO AHORA MISMO por otra IA (NO toques esos archivos):\n" +
                              "\n".join(f"- [{v['perfil']}] {v['titulo']}"
                                         for v in self.en_curso.values()))
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
def _mas_potentes(n=3, tarea="review"):
    """Las cuentas mas capaces con cuota, para que se auditen entre ellas."""
    return ranking(tarea)[:n]


def auditar_proyecto(plan, carpeta, resultados, timeout=600, callback=None,
                     arreglar=True):
    """Cada modelo fuerte revisa lo que escribieron los OTROS y lo corrige.

    No es una opinion sobre un texto: leen los archivos del disco, buscan
    defectos concretos y, si 'arreglar', los arreglan ahi mismo.
    """
    fuertes = _mas_potentes(3)
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
    with ThreadPoolExecutor(max_workers=len(trabajos)) as ex:
        salidas = list(ex.map(
            lambda t: (t[0], correr(t[0], t[1], t[2], "review", timeout, carpeta)),
            trabajos))
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
    with bloqueo():
        d = _leer(MANUAL, {})
        d[pid] = {"usado_pct": float(pct),
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
        return {"pct": m["usado_pct"], "fuente": "declarado",
                "declarado": m["declarado"], "nota": m.get("nota", ""),
                "edad_h": antiguedad_horas(m["declarado"])}
    return {"pct": None, "fuente": "sin dato", "edad_h": None}
