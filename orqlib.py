"""Nucleo compartido del orquestador multi-cuenta.

Estado en disco con bloqueo fcntl: cualquier numero de terminales puede usar
el sistema a la vez sin corromper el ledger ni los contadores.
"""
import json, os, re, shutil, subprocess, time, datetime, fcntl, contextlib

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


def comando(p, prompt):
    prov = p.get("provider")
    if prov == "claude":
        base = ["claude", "-p", "--output-format", "json"]
        if p.get("model"):
            base += ["--model", p["model"]]
        return base + [prompt]
    if prov == "gpt":
        base = ["codex", "exec", "--skip-git-repo-check"]
        if p.get("model"):
            base += ["-m", p["model"]]
        return base + [prompt]
    if prov == "antigravity":
        base = ["agy", "--output-format", "json"]
        if p.get("model"):
            base += ["--model", p["model"]]
        return base + ["-p", prompt]
    if prov == "gemini":
        base = ["gemini", "--skip-trust"]
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
        tok = 0
        m = re.findall(r"tokens used\s*\n\s*([\d,]+)", stderr or "")
        if m:
            tok = int(m[-1].replace(",", ""))
        return (stdout or "").strip(), tok
    return (stdout or "").strip(), 0


# ---------------- ejecucion ----------------
def correr(pid, p, prompt, tarea="reasoning", timeout=300):
    env = entorno(pid, p)
    cmd = comando(p, prompt)
    prov = p.get("provider")
    t0 = time.time()
    f_lock = _lock_proveedor(prov) if prov in SERIALIZAR else None
    try:
        if f_lock:
            fcntl.flock(f_lock, fcntl.LOCK_EX)
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env, cwd=BASE)
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
         "prompt": prompt[:200], "run_id": run_id})
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
