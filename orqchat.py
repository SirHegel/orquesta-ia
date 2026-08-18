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
CTX = os.path.join(L.BASE, "state", "chat_contexto.json")
MAX_CTX = 12          # turnos que se arrastran como contexto


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
    print(f" {D}│{N} {D}escribe lo que necesites.  /ayuda para los comandos{N}")
    print()


AYUDA = f"""
 {B}Comandos{N}   {D}(todo lo demas es una pregunta para la IA){N}
   {C}/cuentas{N}        estado y cuota real de cada cuenta
   {C}/cuota{N}          auditoria de consumo real
   {C}/uso{N}            gasto por dia, semana y mes
   {C}/usar <id>{N}      fijar la cuenta activa de su proveedor
   {C}/en <cuenta>{N}    responder con esa cuenta en vez de la elegida
   {C}/tarea <tipo>{N}   {D}{' '.join(L.TAREAS)}{N}
   {C}/imagen <desc>{N}  generar una imagen en la carpeta actual
   {C}/proyecto <desc>{N} construir un proyecto completo aqui
   {C}/nuevo{N}          empezar una conversacion limpia
   {C}/shell <cmd>{N}    ejecutar un comando del sistema
   {C}/salir{N}
"""


def cargar_ctx():
    try:
        with open(CTX) as f:
            return json.load(f)
    except Exception:
        return []


def guardar_ctx(c):
    try:
        os.makedirs(os.path.dirname(CTX), exist_ok=True)
        with open(CTX, "w") as f:
            json.dump(c[-MAX_CTX:], f, ensure_ascii=False)
    except OSError:
        pass


def con_contexto(ctx, pregunta):
    if not ctx:
        return pregunta
    hist = "\n".join(f"{'Usuario' if t['rol']=='u' else 'Tu'}: {t['txt'][:700]}"
                     for t in ctx[-MAX_CTX:])
    return (f"Esta es la conversacion hasta ahora:\n{hist}\n\n"
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


def responder(pregunta, ctx, tarea, forzado):
    if forzado:
        d = L.disponibles(incluir_bloqueados=True)
        if forzado not in d:
            print(f" {R}·{N} '{forzado}' no esta disponible\n"); return None
        pid, p = forzado, d[forzado]
    else:
        rk = L.ranking(tarea)
        if not rk:
            print(f" {R}·{N} ninguna cuenta disponible ahora mismo\n"); return None
        pid, p = rk[0]["pid"], rk[0]["p"]
    with Girador(f"{pid} pensando…"):
        r = L.correr(pid, p, con_contexto(ctx, pregunta), tarea, 600)
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
    logo()
    cabecera()
    ctx = cargar_ctx()
    tarea, forzado = "reasoning", None
    try:
        while True:
            try:
                linea = input(f" {R}▍{N} ").strip()
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
                if cmd in ("cuentas", "cuota", "uso", "usar", "imagen", "proyecto",
                           "route", "verificar", "permisos"):
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
