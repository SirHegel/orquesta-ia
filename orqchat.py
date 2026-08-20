#!/usr/bin/env python3
"""Orquesta IA — interfaz de lenguaje natural para kitty.

No es un shell con IA encima: es una conversacion. Mantiene contexto entre
turnos aunque cada respuesta la conteste una cuenta distinta.
"""
import os, sys, json, re, time, threading, subprocess, shutil, textwrap, unicodedata
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
DEFAULT_CHAT_TIMEOUT = 1800
MAX_CHAT_TIMEOUT = 21600

# Cada ventana de kitty tiene su propia conversacion. Una ventana nueva
# empieza limpia: no hereda lo que hablaste en otra.
SESION = os.environ.get("ORQ_SESION") or f"suelta-{os.getpid()}"
CTX = os.path.join(DIR_SES, f"{SESION}.json")
CTX_CARPETA = CTX + ".cwd"
CTX_PARCIAL = CTX + ".partial.json"

# Carpeta de trabajo de ESTA ventana. Se conserva si el chat se reinicia.
CARPETA = os.path.abspath(os.environ.get("ORQ_CARPETA") or os.getcwd())
try:
    guardada = open(CTX_CARPETA).read().strip()
    if os.path.isdir(guardada):
        CARPETA = os.path.abspath(guardada)
except OSError:
    pass


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


def _normalizar(txt):
    txt = unicodedata.normalize("NFKD", str(txt or ""))
    txt = "".join(c for c in txt if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", txt).strip()


_OBJETO_IMAGEN = (
    r"(?:imagen(?:es)?|logo(?:s)?|banners?|ilustracion(?:es)?|"
    r"foto(?:s)?|fotografia(?:s)?|portada(?:s)?|miniatura(?:s)?|"
    r"posters?|afiche(?:s)?|flyers?|grafico(?:s)?)"
)
_INICIO_OBJETO_IMAGEN = (
    r"(?:(?:por favor|para mi)\s+)?"
    r"(?:(?:un|una|el|la|los|las|mi|mis|este|esta|estos|estas|"
    r"dos|tres|varios|varias)\s+)?"
    r"(?:(?:nuevo|nueva|bonito|bonita|simple|minimalista|profesional|"
    r"moderno|moderna|elegante|original|fotorrealista|detallado|detallada)\s+){0,2}"
    + _OBJETO_IMAGEN
)
_ORDEN_IMAGEN_DIRECTA = re.compile(
    r"^(?:(?:por favor|oye|ahora)\s+)*"
    r"(?:crea(?:me)?|genera(?:me)?|disena(?:me)?|dibuja(?:me)?|"
    r"haz(?:me)?|produce(?:me)?)\s+" + _INICIO_OBJETO_IMAGEN + r"\b"
)
_ORDEN_IMAGEN_MODAL = re.compile(
    r"^(?:(?:por favor|oye)\s+)?(?:me\s+)?(?:puedes|podrias)\s+"
    r"(?:crear(?:me)?|generar(?:me)?|disenar(?:me)?|dibujar(?:me)?|"
    r"hacer(?:me)?|producir(?:me)?)\s+" + _INICIO_OBJETO_IMAGEN + r"\b"
)
_ORDEN_IMAGEN_DESEO = re.compile(
    r"^(?:(?:por favor|oye)\s+)?(?:quiero|necesito|quisiera)\s+que\s+"
    r"(?:me\s+)?(?:crees|generes|disenes|dibujes|hagas|produzcas)\s+"
    + _INICIO_OBJETO_IMAGEN + r"\b"
)
_SOLICITUD_IMAGEN_DIRECTA = re.compile(
    r"^(?:(?:por favor|oye)\s+)?(?:quiero|necesito|quisiera)\s+"
    + _INICIO_OBJETO_IMAGEN + r"\b"
)
_ORDEN_IMAGEN_INFINITIVA = re.compile(
    r"^(?:por favor\s+)?(?:crear|generar|disenar|dibujar|hacer|producir)\s+"
    + _INICIO_OBJETO_IMAGEN + r"\b"
)


def es_generacion_imagen(pregunta):
    """Reconoce solo peticiones directas de crear un recurso visual estatico.

    El anclaje al inicio evita confundir una explicacion sobre como generar una
    imagen, un script que lo haga o una orden de analizar una imagen existente
    con una solicitud para el backend visual. Video no forma parte de los
    objetos admitidos porque Orquesta no tiene un backend que produzca ese
    archivo.
    """
    p = _normalizar(pregunta)
    if not p:
        return False
    return any(patron.search(p) for patron in (
        _ORDEN_IMAGEN_DIRECTA,
        _ORDEN_IMAGEN_MODAL,
        _ORDEN_IMAGEN_DESEO,
        _SOLICITUD_IMAGEN_DIRECTA,
        _ORDEN_IMAGEN_INFINITIVA,
    ))


def timeout_chat():
    """Limite por intento largo, configurable sin editar codigo."""
    raw = os.environ.get("ORQ_CHAT_TIMEOUT")
    if raw in (None, ""):
        try:
            raw = L.cfg().get("_chat_timeout", DEFAULT_CHAT_TIMEOUT)
        except Exception:
            raw = DEFAULT_CHAT_TIMEOUT
    try:
        valor = int(raw)
    except (TypeError, ValueError):
        valor = DEFAULT_CHAT_TIMEOUT
    return max(60, min(MAX_CHAT_TIMEOUT, valor))


def relevo_automatico():
    """Permite continuar con otra cuenta una vez detenido el proceso anterior."""
    raw = os.environ.get("ORQ_AUTO_HANDOFF")
    if raw in (None, ""):
        try:
            raw = L.cfg().get("_auto_handoff", True)
        except Exception:
            raw = True
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def clasificar_intencion(pregunta):
    """Distingue una consulta, una accion acotada y un proyecto real."""
    p = _normalizar(pregunta)
    if not p:
        return "reasoning"
    if es_generacion_imagen(pregunta):
        return "imagen"
    proyecto = re.search(
        r"\b(termina|terminar|completa|completar|construye|construir|desarrolla|"
        r"desarrollar|implementa|implementar|refactoriza|refactorizar|migra|migrar|"
        r"arregla|arreglar|soluciona|solucionar)\b",
        p,
    )
    alcance = re.search(
        r"\b(proyecto|repo|repositorio|bot|aplicacion|app|sistema|servicio|"
        r"automatizacion|completo|todo|hasta resolver|no (?:pares|puedes parar))\b",
        p,
    )
    if (proyecto and alcance) or re.search(
        r"\b(audita|auditar)\b.*\b(hasta|veces|resolver|resuelto)\b", p
    ):
        return "proyecto"
    if re.search(
        r"\b(arregla|arreglar|corrige|corregir|soluciona|solucionar|modifica|"
        r"modificar|edita|editar|revisa|revisar|audita|auditar|diagnostica|"
        r"diagnosticar|instala|instalar|configura|configurar|ejecuta|ejecutar|"
        r"prueba|probar|verifica|verificar|abre|abrir|crea|crear|elimina|eliminar)\b",
        p,
    ):
        return "agentic"
    return "reasoning"


def _es_reintento(pregunta):
    p = _normalizar(pregunta)
    return bool(re.fullmatch(
        r"(?:vuelve a )?(?:intenta|intentarlo|intentalo|reintenta|reintentarlo|"
        r"continua|continuar|sigue|hazlo)(?: de nuevo| otra vez| nuevamente|"
        r" desde ahi)?(?: por favor)?",
        p,
    ))


def es_navegacion_pura(pregunta):
    """Una orden de cambiar carpeta se resuelve localmente, sin gastar IA."""
    p = _normalizar(pregunta)
    navegacion = bool(
        re.search(r"\b(?:metete|entra|entrar|ve|vete|cambia|cambiar|dirigete) "
                  r"(?:a|al|en|hacia)\b", p)
        or re.search(r"\b(?:dentro|adentro) de\b", p)
        or re.search(r"\b(?:en|a|hacia) (?:la |el )?"
                     r"(?:carpeta|directorio|repo|repositorio|ruta)\b", p)
        or re.search(r"(?<!\w)~?/[A-Za-z0-9._/-]+", pregunta or "")
    )
    trabajo = re.search(
        r"\b(arregla|corrige|soluciona|modifica|edita|revisa|audita|diagnostica|"
        r"instala|configura|ejecuta|prueba|verifica|crea|elimina|termina|completa|"
        r"construye|desarrolla|implementa|refactoriza|migra)\b",
        p,
    )
    ambiguo = re.search(r"\bentra en (?:detalle|materia|contacto)\b", p)
    return bool(navegacion and not trabajo and not ambiguo)


def ultima_peticion(ctx):
    for turno in reversed(ctx):
        if turno.get("rol") == "u" and not _es_reintento(turno.get("txt", "")):
            return turno.get("txt", "")
    return ""


def pregunta_operativa(pregunta, ctx):
    if _es_reintento(pregunta):
        anterior = ultima_peticion(ctx)
        if anterior:
            return anterior + "\n\nContinua desde los cambios que ya existen; auditalos antes de escribir."
    return pregunta


def guardar_carpeta():
    try:
        os.makedirs(DIR_SES, exist_ok=True)
        with open(CTX_CARPETA, "w") as f:
            f.write(CARPETA + "\n")
    except OSError:
        pass


def resolver_carpeta_mencionada(pregunta, actual=None, raices=None):
    """Resuelve una ubicacion explicita sin adivinar por palabras genericas.

    Un nombre comun como ``tests`` o ``web`` puede existir en muchos proyectos.
    Solo buscamos nombres de carpeta cuando el usuario da una indicacion de
    ubicacion o escribe un slug distintivo (por ejemplo ``mi-repo``). Si el
    nombre elegido existe mas de una vez, conservamos la carpeta actual.
    """
    actual = os.path.abspath(actual or CARPETA)
    # Una ruta explicita siempre gana.
    explicitas = re.findall(r"(?<!\w)(~?/[^\s,;]+)", pregunta or "")
    for cruda in reversed(explicitas):
        ruta = os.path.abspath(os.path.expanduser(cruda.rstrip(".:'\"!?)]}")))
        if os.path.isdir(ruta):
            return ruta

    if raices is None:
        home = os.path.expanduser("~")
        raices = [
            os.path.join(home, "Documentos", "Repos"),
            os.path.join(home, "Documentos"),
            os.path.join(home, "Projects"),
            os.path.join(home, "repos"),
        ]
        if actual != home:
            raices.insert(0, actual)

    texto_limpio = _normalizar(pregunta)
    texto = " " + texto_limpio + " "
    hay_indicacion = bool(re.search(
        r"\b(?:metete|entra|entrar|ve|vete|ir|cambia|cambiar|"
        r"ubica|ubicar|dirigete)\s+(?:a|al|en|hacia)\b|"
        r"\b(?:dentro|adentro)\s+de\b|"
        r"\b(?:abre|abrir)\s+(?:la\s+|el\s+)?"
        r"(?:carpeta|directorio|repo|repositorio|ruta)\b|"
        r"\b(?:en|a|hacia)\s+(?:la\s+|el\s+)?"
        r"(?:carpeta|directorio|repo|repositorio|ruta)\b",
        texto_limpio,
    ))
    # Sin una indicacion espacial, solo un slug escrito literalmente justifica
    # cambiar el cwd. La normalizacion permite cotejarlo sin depender de caso.
    slugs = {
        _normalizar(x)
        for x in re.findall(
            r"(?<![\w-])[A-Za-z0-9][A-Za-z0-9.]*"
            r"(?:[-_][A-Za-z0-9.]+)+(?![\w-])",
            pregunta or "",
        )
    }
    if not hay_indicacion and not slugs:
        return None

    omitir = {".git", "node_modules", "__pycache__", ".cache", ".venv", "venv",
              "accounts", "snap", "Trash"}
    stopwords = {"proyecto", "projects", "test", "tests", "src", "source",
                 "web", "app", "bot", "orquesta"}
    por_nombre = {}
    vistas = set()
    for prioridad, raiz in enumerate(raices):
        raiz = os.path.abspath(os.path.expanduser(str(raiz)))
        if not os.path.isdir(raiz) or raiz in vistas:
            continue
        for base, dirs, _files in os.walk(raiz, followlinks=False):
            rel = os.path.relpath(base, raiz)
            profundidad = 0 if rel == "." else rel.count(os.sep) + 1
            dirs[:] = [d for d in dirs if d not in omitir and not d.startswith(".")]
            if profundidad >= 4:
                dirs[:] = []
            vistas.add(base)
            nombre = _normalizar(os.path.basename(base))
            if (len(nombre) < 3 or f" {nombre} " not in texto
                    or (not hay_indicacion and nombre in stopwords)
                    or (not hay_indicacion and nombre not in slugs)):
                continue
            por_nombre.setdefault(nombre, set()).add(os.path.abspath(base))
            if len(vistas) >= 5000:
                break
        if len(vistas) >= 5000:
            break
    if not por_nombre:
        return None

    # En "ve a Documentos, despues a Repos, que esta dentro de Documentos"
    # la ultima mencion es el padre, no el destino. Las transiciones
    # despues/luego/finalmente expresan de forma mas precisa el ultimo `cd`.
    # Conservamos ``-`` y ``_`` mientras extraemos el nombre. Si se usa el
    # texto totalmente normalizado, ``mi-repo`` se parte en dos palabras y
    # una transicion anterior (por ejemplo ``despues a Repos``) gana por
    # accidente aunque el usuario haya dicho luego el repositorio exacto.
    texto_secuencia = unicodedata.normalize("NFKD", str(pregunta or ""))
    texto_secuencia = "".join(
        c for c in texto_secuencia if not unicodedata.combining(c)
    ).lower()
    texto_secuencia = re.sub(r"[^a-z0-9._-]+", " ", texto_secuencia).strip()
    secuencia = re.findall(
        r"\b(?:despues|luego|finalmente)\s+"
        r"(?:(?:metete|entra|ve|vete|dirigete)\s+)?"
        r"(?:a|al|en|hacia)\s+(?:la\s+|el\s+)?([a-z0-9._-]+)",
        texto_secuencia,
    )
    for nombre in reversed(secuencia):
        nombre = _normalizar(nombre)
        if nombre in por_nombre:
            elegido = nombre
            break
    else:
        elegido = None

    # Cuando se nombra una jerarquia ("Documentos, despues Repos, despues X"),
    # el ultimo nombre es el destino. Longitud desempata frases solapadas.
    if elegido is None:
        elegido = max(
            por_nombre,
            key=lambda nombre: (texto_limpio.rfind(nombre), len(nombre)),
        )
    coincidencias = por_nombre[elegido]
    if len(coincidencias) == 1:
        return next(iter(coincidencias))
    repos = [ruta for ruta in coincidencias if os.path.isdir(os.path.join(ruta, ".git"))]
    return repos[0] if len(repos) == 1 else None


def perfil_preferido_chat():
    preferida = os.environ.get("ORQ_CHAT_PERFIL")
    if not preferida:
        try:
            activas = L.activas()
            preferida = activas.get("claude") or activas.get("gpt")
        except Exception:
            preferida = None
    return preferida


def candidatos_para(tarea, forzado=None):
    if forzado:
        disponibles = L.disponibles(incluir_bloqueados=True, tarea=tarea)
        return ([{"pid": forzado, "p": disponibles[forzado]}]
                if forzado in disponibles else [])
    candidatos = L.ranking(tarea)
    preferida = perfil_preferido_chat()
    if preferida:
        candidatos.sort(key=lambda x: x.get("pid") != preferida)
    return candidatos


def _estado_repo(carpeta):
    """Foto barata de archivos para poder reconocer trabajo parcial."""
    try:
        raiz = subprocess.run(
            ["git", "-C", carpeta, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        )
        if raiz.returncode:
            return None, {}
        raiz = raiz.stdout.strip()
        lista = subprocess.run(
            ["git", "-C", raiz, "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True, timeout=8,
        )
        if lista.returncode:
            return raiz, {}
        estado = {}
        for dato in lista.stdout.split(b"\0")[:20000]:
            if not dato:
                continue
            rel = os.fsdecode(dato)
            ruta = os.path.join(raiz, rel)
            try:
                st = os.stat(ruta)
                estado[rel] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
        return raiz, estado
    except (OSError, subprocess.SubprocessError):
        return None, {}


def _cambios_desde(antes, despues):
    _ra, viejo = antes
    _rd, nuevo = despues
    if not _ra or not _rd or os.path.abspath(_ra) != os.path.abspath(_rd):
        return []
    cambios = [p for p, firma in nuevo.items() if viejo.get(p) != firma]
    cambios += [p + " (eliminado)" for p in viejo if p not in nuevo]
    return sorted(cambios)


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
   {C}/tarea <tipo>{N}   {D}auto {' '.join(L.TAREAS)}{N}
   {C}/imagen <desc>{N}  generar una imagen en la carpeta actual
   {C}/proyecto <desc>{N} construir un proyecto completo aqui
   {C}/continuar{N}      retomar el ultimo encargo sobre los cambios existentes
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


def guardar_parcial(pregunta, tarea, resultado):
    dato = {
        "pregunta": pregunta,
        "tarea": tarea,
        "carpeta": CARPETA,
        "perfil": resultado.get("perfil"),
        "run_id": resultado.get("run_id"),
        "session_id": resultado.get("session_id"),
        "archivos": resultado.get("archivos_cambiados") or [],
        "intentos": resultado.get("intentos") or [],
        "ts": time.time(),
    }
    try:
        os.makedirs(DIR_SES, exist_ok=True)
        with open(CTX_PARCIAL, "w") as f:
            json.dump(dato, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def cargar_parcial():
    try:
        with open(CTX_PARCIAL) as f:
            dato = json.load(f)
        return dato if isinstance(dato, dict) else None
    except (OSError, ValueError):
        return None


def limpiar_parcial():
    try:
        os.remove(CTX_PARCIAL)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def parcial_corresponde(pregunta, parcial=None):
    """Evita que una consulta distinta borre un trabajo pendiente."""
    parcial = parcial or cargar_parcial()
    pendiente = _normalizar((parcial or {}).get("pregunta"))
    actual = _normalizar(pregunta)
    return bool(pendiente and (actual == pendiente or actual.startswith(pendiente + " ")))


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
        self.inicio = None
    def __enter__(self):
        if self.vivo:
            self.inicio = time.monotonic()
            self.h = threading.Thread(target=self._girar, daemon=True); self.h.start()
        return self
    def _girar(self):
        marcos = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"; i = 0
        while self.vivo:
            transcurrido = int(time.monotonic() - self.inicio)
            reloj = f"{transcurrido // 60:02d}:{transcurrido % 60:02d}"
            sys.stdout.write(
                f"\r {R}{marcos[i%len(marcos)]}{N} {D}{self.t} · {reloj}{N}   "
            )
            sys.stdout.flush(); i += 1; time.sleep(0.08)
    def __exit__(self, *a):
        if not self.h:
            return
        self.vivo = False; self.h.join(timeout=.3)
        sys.stdout.write("\r" + " " * (len(self.t) + 24) + "\r"); sys.stdout.flush()


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
        f"indique otra ruta. Si la tarea requiere salir de esa carpeta, hazlo. "
        f"No ejecutes git commit ni git push directamente: Orquesta debe publicar "
        f"con su gate de secretos despues de verificar.\n\n")


def prompt_de_relevo(prompt_base, intentos, cambios):
    """Entrega al siguiente motor hechos verificables, no una repeticion ciega."""
    resumen = []
    for intento in intentos[-6:]:
        resumen.append(
            f"- {intento.get('perfil', '?')}: rc={intento.get('rc', '?')}, "
            f"{intento.get('seg', 0)}s, sesion={intento.get('session_id') or 'n/a'}; "
            f"detalle={str(intento.get('detalle') or '')[:240]}"
        )
    archivos = "\n".join(f"- {x}" for x in cambios[:80]) or "- no detectados por Git"
    return (
        prompt_base
        + "\n\nRELEVO CONTROLADO ENTRE MODELOS\n"
        + "El proceso anterior ya termino y no puede seguir escribiendo. No empieces "
          "de cero ni deshagas cambios utiles. Primero inspecciona el estado real del "
          "disco, `git status`, `git diff` y las pruebas disponibles; luego corrige o "
          "continua exactamente el encargo original hasta verificarlo.\n"
        + "Intentos anteriores:\n" + "\n".join(resumen)
        + "\nArchivos que cambiaron desde el inicio:\n" + archivos
        + "\nSi un cambio parcial esta mal, reparalo de forma explicita. Evita duplicar "
          "archivos, servicios o procesos.\n"
    )


def responder(pregunta, ctx, tarea, forzado, reanudar=None):
    if tarea in {"reasoning", "research"}:
        return _responder_sin_lock(pregunta, ctx, tarea, forzado, reanudar)
    with L.bloqueo_proyecto(CARPETA):
        return _responder_sin_lock(pregunta, ctx, tarea, forzado, reanudar)


def _responder_sin_lock(pregunta, ctx, tarea, forzado, reanudar=None):
    forzado_explicito = bool(forzado and not reanudar)
    if reanudar:
        # La misma sesion va primero si sigue disponible. Si su cuota ya quedo
        # bloqueada, el ranking la omite y el relevo arranca directamente con
        # otra cuenta que recibe el estado del disco.
        perfil_anterior = reanudar.get("perfil") or forzado
        candidatos = candidatos_para(tarea)
        if perfil_anterior and not any(
                x.get("pid") == perfil_anterior for x in candidatos):
            anteriores = candidatos_para(tarea, perfil_anterior)
            try:
                esta_bloqueada = bool(L.bloqueado(perfil_anterior))
            except Exception:
                esta_bloqueada = False
            if anteriores and not esta_bloqueada:
                candidatos = anteriores + candidatos
        candidatos.sort(key=lambda x: x.get("pid") != perfil_anterior)
    else:
        candidatos = candidatos_para(tarea, forzado)
    if not candidatos:
        detalle = f"'{forzado}' no esta disponible para {tarea}" if forzado else \
                  "ninguna cuenta disponible ahora mismo"
        print(f" {R}·{N} {detalle}\n")
        return None

    prompt_base = marco_carpeta() + con_contexto(ctx, pregunta)
    prompt = prompt_base
    limite = timeout_chat()
    # Cualquier CLI recibe permisos de herramientas, incluso una consulta que
    # el router etiqueto como reasoning. La foto evita duplicar ediciones si la
    # cuenta termina con error despues de tocar el arbol.
    antes = _estado_repo(CARPETA)
    fallos = []
    intentos = list((reanudar or {}).get("intentos") or [])
    trabajo_acumulado = bool((reanudar or {}).get("archivos"))
    r = None
    for i, candidato in enumerate(candidatos):
        pid, p = candidato["pid"], candidato["p"]
        with Girador(f"{pid} trabajando · limite {limite // 60} min"):
            extra = {}
            if reanudar and pid == reanudar.get("perfil") and reanudar.get("session_id"):
                extra = {"session_id": reanudar["session_id"], "resume": True}
            if tarea in {"reasoning", "research"}:
                extra["solo_lectura"] = True
            intento = L.correr(
                pid, p, prompt, tarea, limite, carpeta=CARPETA, **extra
            )
        texto = (intento.get("texto") or "").strip()
        if intento.get("rc") == 0 and texto and not texto.startswith("[ERROR"):
            r = intento
            break
        fallos.append((pid, texto or f"rc={intento.get('rc', '?')}"))
        cambios = _cambios_desde(antes, _estado_repo(CARPETA))
        hubo_trabajo = bool(
            cambios
            or intento.get("tokens")
            or float(intento.get("seg") or 0) >= 30
        )
        trabajo_acumulado = trabajo_acumulado or hubo_trabajo
        intentos.append({
            "perfil": pid,
            "rc": intento.get("rc"),
            "seg": intento.get("seg", 0),
            "session_id": intento.get("session_id"),
            "run_id": intento.get("run_id"),
            "detalle": texto[:400],
        })
        intento["intentos"] = intentos
        puede_relevar = (
            relevo_automatico()
            and not forzado_explicito
            and i + 1 < len(candidatos)
        )
        if puede_relevar:
            intento["estado"] = "parcial" if trabajo_acumulado else "fallo"
            intento["archivos_cambiados"] = cambios
            pregunta_base = (reanudar or {}).get("pregunta") or pregunta
            guardar_parcial(pregunta_base, tarea, intento)
            siguiente = candidatos[i + 1]["pid"]
            motivo = ("alcanzo el limite de tiempo" if intento.get("rc") == 124
                      else "agoto su cuota" if intento.get("limitado")
                      else "no pudo completar el intento")
            print(f" {Y}·{N} {pid} {motivo}; relevo seguro a {siguiente}…")
            if cambios:
                print(f"   {D}{len(cambios)} cambio(s) conservado(s); "
                      f"{siguiente} los auditara antes de continuar.{N}")
            prompt = prompt_de_relevo(prompt_base, intentos, cambios)
            continue

        if intento.get("rc") == 124 or trabajo_acumulado:
            intento["estado"] = "parcial"
            intento["archivos_cambiados"] = cambios
            pregunta_base = (reanudar or {}).get("pregunta") or pregunta
            guardar_parcial(pregunta_base, tarea, intento)
            if intento.get("rc") == 124:
                print(f" {Y}▍{N} trabajo interrumpido al alcanzar {limite // 60} min")
            else:
                print(f" {Y}▍{N} {pid} termino con error despues de trabajar")
            print(f"   {D}se hicieron {len(fallos)} intento(s) secuenciales; "
                  f"no queda ningun escritor anterior activo.{N}")
            if cambios:
                print(f"   {G}cambios parciales conservados:{N}")
                for archivo in cambios[:12]:
                    print(f"     {D}· {archivo}{N}")
                if len(cambios) > 12:
                    print(f"     {D}· y {len(cambios) - 12} mas{N}")
            print(f"   {D}usa /continuar para auditar y retomar desde esos cambios.{N}\n")
            return intento
        if forzado:
            break
        # Solo cambiar de motor ante un fallo temprano, sin consumo ni cambios.
        # Una marca de cuota no vuelve seguro repetir un escritor que ya trabajo.
        reintentable = (intento.get("rc") in (1, 2, 127)
                        and not intento.get("tokens")
                        and not cambios
                        and float(intento.get("seg") or 0) < 30)
        if not reintentable:
            break
        if i + 1 < len(candidatos):
            print(f" {Y}·{N} {pid} no respondio; probando {candidatos[i + 1]['pid']}…")

    if r is None:
        agotadas = len(fallos) == len(candidatos)
        titulo = ("ninguna de las cuentas intentadas pudo completar el encargo"
                  if agotadas and len(fallos) > 1
                  else "la cuenta ejecutada no pudo completar el encargo")
        print(f" {R}▍{N} {titulo}")
        for pid, detalle in fallos:
            print(f"   {D}{pid}: {detalle[:180]}{N}")
        print()
        return None

    pid = r["perfil"]
    if intentos:
        r["intentos"] = intentos
    cambios = _cambios_desde(antes, _estado_repo(CARPETA))
    if cambios:
        r["archivos_cambiados"] = cambios
    if reanudar or parcial_corresponde(pregunta):
        limpiar_parcial()
    print(f" {R}▍{N} {D}{pid}{N}")
    print(envolver(r["texto"] or "(sin respuesta)"))
    print(f"   {D}{r['tokens']:,} tok · {r['seg']}s{N}\n")
    return r


def ejecutar_proyecto_chat(descripcion, ctx, preferir=None):
    """Delega un encargo amplio al flujo multi-IA con progreso visible."""
    os.makedirs(DIR_SES, exist_ok=True)
    tmp = os.path.join(DIR_SES, f"{SESION}.ctx.txt")
    try:
        with open(tmp, "w") as f:
            f.write("\n".join(
                f"{'Usuario' if t.get('rol') == 'u' else 'IA'}: {t.get('txt', '')[:700]}"
                for t in ctx[-10:]
            ))
    except OSError as e:
        print(f" {R}·{N} no pude preparar el contexto: {e}\n")
        return 1
    cmd = [
        os.path.join(L.BASE, "orq"), "proyecto", descripcion,
        "--en", CARPETA, "--si", "--timeout", str(timeout_chat()),
        "--contexto", tmp,
    ]
    preferir = preferir or perfil_preferido_chat()
    if preferir:
        cmd += ["--preferir", preferir]
    try:
        return subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        print(f"\n {Y}·{N} proyecto interrumpido por el usuario; los cambios se conservan\n")
        return 130
    except OSError as e:
        print(f" {R}·{N} no pude iniciar el proyecto: {e}\n")
        return 1


def ejecutar_imagen_chat(descripcion, perfil=None):
    """Delega al flujo visual del CLI, que verifica el archivo generado."""
    cmd = [
        os.path.join(L.BASE, "orq"), "imagen", descripcion,
        "--en", CARPETA,
    ]
    if perfil:
        cmd += ["--perfil", perfil]
    try:
        return subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        print(f"\n {Y}·{N} generacion de imagen interrumpida por el usuario\n")
        return 130
    except OSError as e:
        print(f" {R}·{N} no pude iniciar el flujo de imagen: {e}\n")
        return 1


def principal():
    global CARPETA
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
    try:
        os.chdir(CARPETA)
    except OSError:
        pass
    tarea, forzado = None, os.environ.get("ORQ_CHAT_PERFIL") or None
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
                    if arg:
                        nueva = os.path.abspath(os.path.expanduser(arg))
                        if os.path.isdir(nueva):
                            CARPETA = nueva
                            os.chdir(CARPETA)
                            guardar_carpeta()
                            ctx = []; guardar_ctx(ctx)
                            limpiar_parcial()
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
                    ctx = []; guardar_ctx(ctx); limpiar_parcial()
                    print(f" {G}·{N} conversacion nueva\n"); continue
                if cmd == "tarea":
                    if arg == "auto":
                        tarea = None; print(f" {G}·{N} tarea: deteccion automatica\n")
                    elif arg in L.TAREAS:
                        tarea = arg; print(f" {G}·{N} tarea: {tarea}\n")
                    else:
                        print(f" {D}tareas: auto {' '.join(L.TAREAS)}{N}\n")
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
                    ejecutar_proyecto_chat(arg, ctx, preferir=forzado)
                    print(); continue
                if cmd == "continuar":
                    parcial = cargar_parcial() or {}
                    encargo = parcial.get("pregunta") or ultima_peticion(ctx)
                    if not encargo:
                        print(f" {D}no hay un encargo anterior para continuar{N}\n"); continue
                    destino = parcial.get("carpeta")
                    if destino and os.path.isdir(destino):
                        CARPETA = os.path.abspath(destino); os.chdir(CARPETA); guardar_carpeta()
                    instruccion = (encargo +
                                   "\n\nContinua exactamente desde donde quedaste. Primero "
                                   "audita y conserva los cambios parciales existentes; termina "
                                   "las pruebas y entrega un informe verificable.")
                    perfil = parcial.get("perfil") or forzado
                    if parcial.get("session_id") and perfil:
                        detalle = f"la misma sesion de {perfil}"
                    elif perfil:
                        detalle = f"la misma cuenta {perfil} sobre los cambios existentes"
                    else:
                        detalle = "una cuenta sobre los cambios existentes"
                    print(f" {Y}·{N} reanudando {detalle} desde {CARPETA}\n")
                    r = responder(instruccion, ctx, "agentic", perfil, parcial or None)
                    if r and r.get("rc") == 0:
                        limpiar_parcial()
                        if r.get("texto"):
                            ctx.append({"rol": "a", "txt": r["texto"]})
                        guardar_ctx(ctx)
                    print(); continue
                if cmd == "usar":
                    if not arg:
                        print(f" {D}uso: /usar <id>{N}\n"); continue
                    rc = subprocess.run([os.path.join(L.BASE, "orq"), "usar", arg]).returncode
                    if rc == 0:
                        forzado = arg
                        print(f" {G}·{N} esta conversacion queda fijada a {arg}\n")
                    continue
                if cmd == "imagen":
                    if not arg:
                        print(f" {D}que imagen quieres generar?{N}\n"); continue
                    ejecutar_imagen_chat(arg, perfil=forzado)
                    print(); continue
                if cmd in ("cuentas", "cuota", "global", "uso", "usar",
                           "route", "verificar", "permisos", "equipo"):
                    sub = [os.path.join(L.BASE, "orq"), cmd]
                    if arg:
                        sub += ([arg] if cmd in ("usar", "route") else [arg, "--si"]
                                if cmd == "proyecto" else [arg])
                    subprocess.run(sub)
                    print(); continue
                print(f" {D}no conozco /{cmd}. /ayuda{N}\n"); continue

            parcial_reintento = cargar_parcial() if _es_reintento(linea) else None
            if parcial_reintento and parcial_reintento.get("pregunta"):
                destino = parcial_reintento.get("carpeta")
                if destino and os.path.isdir(destino):
                    CARPETA = os.path.abspath(destino)
                    os.chdir(CARPETA)
                    guardar_carpeta()
                operativa = (parcial_reintento["pregunta"] +
                              "\n\nContinua exactamente desde la sesion y los cambios "
                              "existentes; audita antes de escribir y termina las pruebas.")
            else:
                operativa = pregunta_operativa(linea, ctx)
            detectada = resolver_carpeta_mencionada(operativa, CARPETA)
            if detectada and os.path.abspath(detectada) != CARPETA:
                CARPETA = os.path.abspath(detectada)
                os.chdir(CARPETA)
                guardar_carpeta()
                print(f" {G}·{N} carpeta detectada: {CARPETA}\n")
            if detectada and es_navegacion_pura(operativa):
                corta = CARPETA.replace(os.path.expanduser("~"), "~")
                respuesta_local = f"Carpeta activa: {corta}"
                print(f" {G}·{N} {respuesta_local}\n")
                ctx.append({"rol": "u", "txt": linea})
                ctx.append({"rol": "a", "txt": respuesta_local})
                guardar_ctx(ctx)
                continue
            tipo = "agentic" if parcial_reintento else (tarea or clasificar_intencion(operativa))
            ctx.append({"rol": "u", "txt": linea})
            if tipo == "imagen" and not parcial_reintento:
                rc = ejecutar_imagen_chat(operativa, perfil=forzado)
                ctx.append({
                    "rol": "a",
                    "txt": ("Flujo de imagen ejecutado; el resultado verificable se "
                            f"mostro en la terminal (rc={rc})."),
                })
                guardar_ctx(ctx)
                continue
            # /en es una orden de usar una sola cuenta; no se ignora aunque el
            # texto describa un proyecto amplio. /proyecto sigue disponible
            # cuando se desea el flujo multi-IA explicito.
            if tipo == "proyecto" and not forzado and not parcial_reintento:
                print(f" {Y}·{N} encargo amplio detectado: proyecto multi-IA"
                      f" · limite {timeout_chat() // 60} min por fase\n")
                rc = ejecutar_proyecto_chat(operativa, ctx[:-1])
                resumen = ("Proyecto ejecutado y auditado." if rc == 0 else
                           f"Proyecto incompleto (rc={rc}); revisa el detalle mostrado. "
                           "Se conservaron sus cambios.")
                ctx.append({"rol": "a", "txt": resumen})
                if rc == 0:
                    limpiar_parcial()
                guardar_ctx(ctx)
                continue
            tarea_ejecucion = "agentic" if tipo == "proyecto" else tipo
            perfil = ((parcial_reintento or {}).get("perfil") or forzado)
            r = responder(operativa, ctx[:-1], tarea_ejecucion, perfil,
                          parcial_reintento)
            if r and r.get("rc") == 0 and r.get("texto"):
                ctx.append({"rol": "a", "txt": r["texto"]})
            elif r and r.get("estado") == "parcial":
                ctx.append({"rol": "a", "txt":
                            "Trabajo parcial conservado; continua auditando los cambios existentes."})
            guardar_ctx(ctx)
    finally:
        guardar_ctx(ctx)
        quitar_logo()
        print(f" {D}hasta luego{N}\n")


if __name__ == "__main__":
    principal()
