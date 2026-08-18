#!/usr/bin/env python3
"""Orquesta IA — interfaz de lenguaje natural para kitty.

No es un shell con IA encima: es una conversacion. Mantiene contexto entre
turnos aunque cada respuesta la conteste una cuenta distinta.
"""
import os, sys, json, re, time, threading, subprocess, shutil, textwrap
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import orqlib as L

try:
    import readline
except ImportError:
    readline = None

R, D, N, B = "\033[38;2;224;50;46m", "\033[2m", "\033[0m", "\033[1m"
G, Y, C = "\033[38;2;154;160;142m", "\033[38;2;201;185;138m", "\033[38;2;122;162;200m"
HIST = os.path.join(L.BASE, "state", "chat_historial")
DIR_SES = os.path.join(L.BASE, "state", "sesiones")
DIR_MEM = os.path.join(L.BASE, "memoria")
MAX_CTX = 14          # turnos que se arrastran dentro de ESTA ventana

# Cada ventana de kitty tiene su propia conversacion. Una ventana nueva
# empieza limpia: no hereda lo que hablaste en otra.
SESION = os.environ.get("ORQ_SESION") or f"suelta-{os.getpid()}"
CTX = os.path.join(DIR_SES, f"{SESION}.json")

# Carpeta de trabajo de ESTA ventana. Todo lo que hagan las IA ocurre aqui
# dentro; no ven ni tocan el resto del equipo salvo que se lo pidas.
CARPETA = os.path.abspath(os.environ.get("ORQ_CARPETA") or os.getcwd())


def prompt_entrada():
    """Prompt visible de tres columnas, sin confundir el cursor de Readline.

    Readline necesita \001/\002 alrededor de cada secuencia ANSI no imprimible.
    Sin esos marcadores calcula mal el ancho, vuelve al inicio antes de tiempo y
    sobreescribe los prompts largos al envolverlos en Kitty.
    """
    if readline:
        return f" \001{R}\002▍\001{N}\002 "
    return f" {R}▍{N} "


def ancho():
    return shutil.get_terminal_size((100, 30)).columns


def en_kitty():
    return bool(os.environ.get("KITTY_WINDOW_ID")) and os.environ.get("TERM") != "dumb"


def logo():
    """Musashi en la esquina, como en tu claude-vagabond."""
    img = os.path.expanduser("~/.config/kitty/musashi.png")
    if not en_kitty() or not os.path.exists(img):
        return
    try:
        subprocess.run(["kitten", "@", "set-window-logo", "--self",
                        "--position", "top-right", "--alpha",
                        os.environ.get("ORQ_LOGO_ALPHA", "0.30"), img],
                       capture_output=True, timeout=5)
    except Exception:
        pass


def quitar_logo():
    if not en_kitty():
        return
    try:
        subprocess.run(["kitten", "@", "set-window-logo", "--self", "none"],
                       capture_output=True, timeout=5)
    except Exception:
        pass


def cabecera():
    w = ancho()
    cuentas = [(pid, p) for pid, p in L.cfg().get("profiles", {}).items()
               if L.autenticado(pid, p) and p.get("enabled", True)]
    print()
    print(f" {R}▍{N}{B} ORQUESTA IA {N}{D}· lenguaje natural · potencia maxima{N}")
    piezas = []
    for pid, p in cuentas:
        try:
            q = L.cuota(pid, p)
        except Exception:
            q = {}
        pct = q.get("usado_pct")
        if pct is not None and q.get("fuente") == "proveedor":
            col = R if pct >= 80 else (Y if pct >= 50 else G)
            piezas.append(f"{D}{pid}{N} {col}{pct:.0f}%{N}")
        else:
            piezas.append(f"{D}{pid}{N} {G}·{N}")
    print(f" {D}│{N} " + f"  {D}·{N}  ".join(piezas))
    mem = memoria()
    extra = f"  {D}·{N}  {D}memoria:{N} {len(mem)} nota{'s' if len(mem)!=1 else ''}" if mem else ""
    corta = CARPETA.replace(os.path.expanduser("~"), "~")
    print(f" {D}│{N} {D}carpeta:{N} {corta}")
    print(f" {D}│{N} {D}ventana nueva, conversacion limpia{N}{extra}")
    print(f" {D}│{N} {D}escribe lo que necesites.  /ayuda para los comandos{N}")
    print()


AYUDA = f"""
 {B}Comandos{N}   {D}(todo lo demas es una pregunta para la IA){N}
   {C}/cuentas{N}        estado y cuota real de cada cuenta
   {C}/cuota{N}          auditoria de consumo real
   {C}/global{N}         porcentaje global y vigencia de cada cuota
   {C}/uso{N}            gasto por dia, semana y mes
   {C}/usar <id>{N}      fijar la cuenta activa de su proveedor
   {C}/en <cuenta>{N}    responder con esa cuenta en vez de la elegida
   {C}/tarea <tipo>{N}   {D}{' '.join(L.TAREAS)}{N}
   {C}/imagen <desc>{N}  generar una imagen en la carpeta actual
   {C}/proyecto <desc>{N} construir un proyecto completo aqui
   {C}/carpeta [ruta]{N} desde donde trabaja esta ventana (acceso al PC completo)
   {C}/equipo{N}         que sabe del hardware y las herramientas de la maquina
   {C}/memoria{N}        ver lo que Orquesta sabe siempre
   {C}/recuerda <txt>{N} asignarle un hecho permanente
   {C}/olvida <arch>{N}  quitarlo de la memoria
   {C}/nuevo{N}          empezar una conversacion limpia
   {C}/shell <cmd>{N}    ejecutar un comando del sistema
   {C}/salir{N}
"""


def cargar_ctx():
    """Solo recupera el contexto de ESTA ventana (por si se reinicio el chat)."""
    try:
        with open(CTX) as f:
            return json.load(f)
    except Exception:
        return []


def limpiar_sesiones_viejas(dias=2):
    """Borra conversaciones de ventanas que ya no existen."""
    try:
        corte = time.time() - dias * 86400
        for f in os.listdir(DIR_SES):
            ruta = os.path.join(DIR_SES, f)
            if os.path.getmtime(ruta) < corte:
                os.remove(ruta)
    except OSError:
        pass


def memoria():
    """Lo que Orquesta sabe siempre, porque tu se lo asignaste."""
    trozos = []
    if not os.path.isdir(DIR_MEM):
        return trozos
    for f in sorted(os.listdir(DIR_MEM)):
        if not f.endswith(".md") or f == "README.md":
            continue
        try:
            txt = open(os.path.join(DIR_MEM, f)).read().strip()
            if txt:
                trozos.append((f[:-3], txt))
        except OSError:
            pass
    return trozos


def recordar(hecho):
    os.makedirs(DIR_MEM, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", hecho.lower())[:40].strip("-") or "nota"
    ruta = os.path.join(DIR_MEM, f"{slug}.md")
    n = 1
    while os.path.exists(ruta):
        ruta = os.path.join(DIR_MEM, f"{slug}-{n}.md"); n += 1
    with open(ruta, "w") as f:
        f.write(hecho.strip() + "\n")
    return os.path.basename(ruta)


def guardar_ctx(c):
    try:
        os.makedirs(DIR_SES, exist_ok=True)
        with open(CTX, "w") as f:
            json.dump(c[-MAX_CTX:], f, ensure_ascii=False)
    except OSError:
        pass


def con_contexto(ctx, pregunta):
    mem = memoria()
    cabeza = ""
    if mem:
        cabeza = ("Contexto permanente que el usuario te asigno:\n" +
                  "\n".join(f"[{n}] {t[:900]}" for n, t in mem) + "\n\n")
    if not ctx:
        return (cabeza + pregunta) if cabeza else pregunta
    hist = "\n".join(f"{'Usuario' if t['rol']=='u' else 'Tu'}: {t['txt'][:700]}"
                     for t in ctx[-MAX_CTX:])
    return (cabeza + f"Esta es la conversacion hasta ahora:\n{hist}\n\n"
            f"Usuario: {pregunta}\n\n"
            f"Responde solo al ultimo mensaje, en el mismo idioma, sin repetir "
            f"lo ya dicho y sin saludar de nuevo.")


class Girador:
    def __init__(self, texto):
        self.t = texto
        self.vivo = sys.stdout.isatty()      # sin terminal real, no animar
        self.h = None
    def __enter__(self):
        if self.vivo:
            self.h = threading.Thread(target=self._girar, daemon=True); self.h.start()
        return self
    def _girar(self):
        marcos = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"; i = 0
        while self.vivo:
            sys.stdout.write(f"\r {R}{marcos[i%len(marcos)]}{N} {D}{self.t}{N}   ")
            sys.stdout.flush(); i += 1; time.sleep(0.08)
    def __exit__(self, *a):
        if not self.h:
            return
        self.vivo = False; self.h.join(timeout=.3)
        sys.stdout.write("\r" + " " * (len(self.t) + 12) + "\r"); sys.stdout.flush()


def envolver(txt, sangria="   "):
    w = max(48, ancho() - 6)
    out = []
    for linea in (txt or "").split("\n"):
        if not linea.strip():
            out.append("")
        elif len(linea) <= w:
            out.append(sangria + linea)
        else:
            out += [sangria + x for x in textwrap.wrap(linea, w) or [""]]
    return "\n".join(out)


def marco_carpeta():
    """Alcance: control total del equipo, con foco en la carpeta abierta."""
    try:
        pc = L.contexto_equipo()
    except Exception:
        pc = ""
    return (
        f"{pc}\n"
        f"Tienes acceso completo a este equipo y permisos ya concedidos: puedes "
        f"leer, escribir, instalar, ejecutar comandos y construir lo que haga falta "
        f"en cualquier parte del sistema.\n"
        f"Estas trabajando desde: {CARPETA}\n"
        f"Empieza por ahi y deja ahi lo que construyas, salvo que el usuario "
        f"indique otra ruta. Si la tarea requiere salir de esa carpeta, hazlo.\n\n")


def responder(pregunta, ctx, tarea, forzado):
    if forzado:
        d = L.disponibles(incluir_bloqueados=True, tarea=tarea)
        if forzado not in d:
            print(f" {R}·{N} '{forzado}' no esta disponible para {tarea}\n"); return None
        candidatos = [{"pid": forzado, "p": d[forzado]}]
    else:
        candidatos = L.ranking(tarea)
        if not candidatos:
            print(f" {R}·{N} ninguna cuenta disponible ahora mismo\n"); return None

    prompt = marco_carpeta() + con_contexto(ctx, pregunta)
    fallos = []
    r = None
    for i, candidato in enumerate(candidatos):
        pid, p = candidato["pid"], candidato["p"]
        with Girador(f"{pid} pensando…"):
            intento = L.correr(pid, p, prompt, tarea, 600, carpeta=CARPETA)
        texto = (intento.get("texto") or "").strip()
        if intento.get("rc") == 0 and texto and not texto.startswith("[ERROR"):
            r = intento
            break
        fallos.append((pid, texto or f"rc={intento.get('rc', '?')}"))
        if forzado:
            break
        # Solo cambiar de motor ante un fallo temprano y sin consumo. Un
        # timeout pudo alcanzar a modificar archivos; repetirlo seria riesgoso.
        reintentable = (bool(intento.get("limitado"))
                        or (intento.get("rc") in (1, 2, 127)
                            and not intento.get("tokens")
                            and float(intento.get("seg") or 0) < 30))
        if not reintentable:
            break
        if i + 1 < len(candidatos):
            print(f" {Y}·{N} {pid} no respondio; probando {candidatos[i + 1]['pid']}…")

    if r is None:
        print(f" {R}▍{N} ninguna cuenta pudo responder")
        for pid, detalle in fallos:
            print(f"   {D}{pid}: {detalle[:180]}{N}")
        print()
        return None

    pid = r["perfil"]
    print(f" {R}▍{N} {D}{pid}{N}")
    print(envolver(r["texto"] or "(sin respuesta)"))
    print(f"   {D}{r['tokens']:,} tok · {r['seg']}s{N}\n")
    return r


def principal():
    if readline:
        try:
            readline.read_history_file(HIST)
        except OSError:
            pass
        readline.set_history_length(2000)
    limpiar_sesiones_viejas()
    logo()
    cabecera()
    ctx = cargar_ctx()
    tarea, forzado = "reasoning", None
    try:
        while True:
            try:
                linea = input(prompt_entrada()).strip()
            except EOFError:
                print(); break
            except KeyboardInterrupt:
                print(f"\n {D}(/salir para terminar){N}"); continue
            if not linea:
                continue
            if readline:
                try:
                    readline.write_history_file(HIST)
                except OSError:
                    pass

            if linea.startswith("/"):
                partes = linea[1:].split(None, 1)
                cmd = partes[0].lower()
                arg = partes[1] if len(partes) > 1 else ""
                if cmd in ("salir", "exit", "q"):
                    break
                if cmd == "ayuda":
                    print(AYUDA); continue
                if cmd == "carpeta":
                    global CARPETA
                    if arg:
                        nueva = os.path.abspath(os.path.expanduser(arg))
                        if os.path.isdir(nueva):
                            CARPETA = nueva
                            os.chdir(CARPETA)
                            ctx = []; guardar_ctx(ctx)
                            print(f" {G}·{N} carpeta: {CARPETA}")
                            print(f" {D}  conversacion reiniciada para este contexto{N}\n")
                        else:
                            print(f" {R}·{N} no existe: {nueva}\n")
                    else:
                        try:
                            n = len(os.listdir(CARPETA))
                        except OSError:
                            n = "?"
                        print(f" {D}carpeta:{N} {CARPETA}  {D}({n} elementos){N}\n")
                    continue
                if cmd == "memoria":
                    mem = memoria()
                    if not mem:
                        print(f" {D}sin memoria asignada. Usa /recuerda <hecho>{N}\n")
                    else:
                        print(f"\n {B}Memoria permanente{N} {D}({DIR_MEM}){N}")
                        for n, t in mem:
                            print(f"   {R}·{N} {B}{n}{N}")
                            print(envolver(t[:400], "     " + D) + N)
                        print()
                    continue
                if cmd == "recuerda":
                    if not arg:
                        print(f" {D}que quieres que recuerde?{N}\n"); continue
                    f = recordar(arg)
                    print(f" {G}·{N} guardado en memoria/{f}\n"); continue
                if cmd == "olvida":
                    ruta = os.path.join(DIR_MEM, arg if arg.endswith(".md") else arg + ".md")
                    if os.path.exists(ruta):
                        os.remove(ruta); print(f" {G}·{N} olvidado: {arg}\n")
                    else:
                        print(f" {D}no encuentro '{arg}'. Mira /memoria{N}\n")
                    continue
                if cmd == "nuevo":
                    ctx = []; guardar_ctx(ctx)
                    print(f" {G}·{N} conversacion nueva\n"); continue
                if cmd == "tarea":
                    if arg in L.TAREAS:
                        tarea = arg; print(f" {G}·{N} tarea: {tarea}\n")
                    else:
                        print(f" {D}tareas: {' '.join(L.TAREAS)}{N}\n")
                    continue
                if cmd == "en":
                    forzado = arg or None
                    print(f" {G}·{N} " + (f"respondera {forzado}" if forzado
                                          else "vuelve a elegir el router") + "\n")
                    continue
                if cmd == "shell":
                    if arg:
                        subprocess.run(arg, shell=True)
                    print(); continue
                if cmd == "proyecto":
                    if not arg:
                        print(f" {D}que quieres construir?{N}\n"); continue
                    # el proyecto hereda lo que se ha hablado en esta ventana
                    tmp = os.path.join(DIR_SES, f"{SESION}.ctx.txt")
                    with open(tmp, "w") as f:
                        f.write("\n".join(
                            f"{'Usuario' if t['rol']=='u' else 'IA'}: {t['txt'][:500]}"
                            for t in ctx[-10:]))
                    subprocess.run([os.path.join(L.BASE, "orq"), "proyecto", arg,
                                    "--en", CARPETA, "--si", "--contexto", tmp])
                    print(); continue
                if cmd in ("cuentas", "cuota", "global", "uso", "usar", "imagen",
                           "route", "verificar", "permisos", "equipo"):
                    sub = [os.path.join(L.BASE, "orq"), cmd]
                    if arg:
                        sub += ([arg] if cmd in ("usar", "route") else [arg, "--si"]
                                if cmd == "proyecto" else [arg])
                    subprocess.run(sub)
                    print(); continue
                print(f" {D}no conozco /{cmd}. /ayuda{N}\n"); continue

            ctx.append({"rol": "u", "txt": linea})
            r = responder(linea, ctx[:-1], tarea, forzado)
            if r and r["texto"]:
                ctx.append({"rol": "a", "txt": r["texto"]})
            guardar_ctx(ctx)
    finally:
        guardar_ctx(ctx)
        quitar_logo()
        print(f" {D}hasta luego{N}\n")


if __name__ == "__main__":
    principal()
