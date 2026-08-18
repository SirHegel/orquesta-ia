"""Nucleo compartido del orquestador multi-cuenta.

Estado en disco con bloqueo fcntl: cualquier numero de terminales puede usar
el sistema a la vez sin corromper el ledger ni los contadores.
"""
import json, os, re, subprocess, time, datetime, fcntl, contextlib

BASE = os.environ.get("ORQ_HOME") or os.path.dirname(os.path.abspath(__file__))
ACCOUNTS = os.path.join(BASE, "accounts")
PROFILES = os.path.join(BASE, "profiles.json")
LEDGER = os.path.join(BASE, "state", "ledger.jsonl")
LIMITS = os.path.join(BASE, "state", "limits.json")
SCORES = os.path.join(BASE, "state", "scores.json")
LOCK = os.path.join(BASE, "state", ".lock")

TAREAS = ["code", "agentic", "reasoning", "review", "writing",
          "research", "edicion", "bulk"]

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
    if prov == "claude":
        return f'CLAUDE_CONFIG_DIR="{h}" claude   # dentro escribe: /login'
    if prov == "gpt":
        return f'CODEX_HOME="{h}" codex login'
    if prov == "gemini":
        if p.get("auth") == "oauth":
            return (f'GEMINI_CLI_HOME="{h}" GOOGLE_GENAI_USE_GCA=true BROWSER=firefox gemini'
                    f'   # autoriza con la cuenta de Google')
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
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env, cwd=BASE)
        out, err, rc = r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        out, err, rc = "", f"timeout tras {timeout}s", 124
    except FileNotFoundError as e:
        out, err, rc = "", f"binario no encontrado: {e}", 127
    dur = round(time.time() - t0, 1)
    texto, tok = extraer(p["provider"], out, err)
    lim = detectar_limite(pid, p, (texto or "") + "\n" + (err or ""))
    if rc != 0 and not texto:
        texto = f"[ERROR rc={rc}] {(err or '').strip()[:400]}"
    run_id = f"{pid}-{int(t0)}"
    log({"ts": ahora().isoformat(timespec="seconds"), "fecha": hoy(),
         "perfil": pid, "provider": p["provider"], "tarea": tarea,
         "tokens": tok, "seg": dur, "rc": rc, "limite": bool(lim),
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
