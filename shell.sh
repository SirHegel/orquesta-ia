# --- Orquesta IA · integracion opcional para shells Bash interactivos ---
# `orq chat` no necesita este archivo. Si se carga desde ~/.bashrc, no debe
# arrancar nada por defecto ni intervenir en shells no interactivos.
case $- in
  *i*) ;;
  *) return 0 2>/dev/null || exit 0 ;;
esac

# ORQ_HOME selecciona una instalación completa, no un directorio de datos.
# Cargar este archivo es la decisión más reciente del operador: su ubicación
# real reemplaza cualquier ORQ_HOME heredado y todos los entrypoints la usan.
_ORQ_SHELL_FILE="${BASH_SOURCE[0]:-}"
if [ ! -x /usr/bin/python3 ]; then
  printf 'orquesta: falta /usr/bin/python3 de confianza\n' >&2
  return 1 2>/dev/null || exit 1
fi
if ! ORQ_HOME="$(
  /usr/bin/python3 -I - "$_ORQ_SHELL_FILE" <<'PY'
import os
import sys

archivo = sys.argv[1]
raiz_shell = os.path.dirname(os.path.realpath(archivo))
sys.path.insert(0, raiz_shell)
try:
    import orqroot
    raiz = orqroot.resolver_raiz(raiz_shell, "shell.sh")
except (ImportError, ValueError) as exc:
    print(exc, file=sys.stderr)
    raise SystemExit(1)
print(raiz)
PY
)"; then
  unset _ORQ_SHELL_FILE ORQ_HOME
  return 1 2>/dev/null || exit 1
fi
export ORQ_HOME
_ORQ_HOME_CANONICO="$ORQ_HOME"
unset _ORQ_SHELL_FILE
if ! _ORQ_USER_HOME="$(/usr/bin/python3 -I - <<'PY'
import os
import pwd
print(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))
PY
)" || [ "${_ORQ_USER_HOME#/}" = "$_ORQ_USER_HOME" ]; then
  unset ORQ_HOME _ORQ_HOME_CANONICO _ORQ_USER_HOME
  return 1 2>/dev/null || exit 1
fi
case ":$PATH:" in
  *":$_ORQ_USER_HOME/.local/bin:"*) ;;
  *) export PATH="$_ORQ_USER_HOME/.local/bin:$PATH";;
esac

# Detecta el cambio antes de reemplazar la selección anterior. El entorno
# original se captura antes de cargar cualquier configuración de Orquesta.
if [ -n "${_ORQ_RAIZ_ACTIVA:-}" ] && [ "$_ORQ_RAIZ_ACTIVA" != "$ORQ_HOME" ]; then
  _ORQ_CAMBIO_RAIZ=1
else
  _ORQ_CAMBIO_RAIZ=0
fi
declare -ga _ORQ_VARIABLES_PROVEEDOR=(
  CLAUDE_CONFIG_DIR CODEX_HOME GEMINI_CLI_HOME GEMINI_API_KEY
  GOOGLE_API_KEY GOOGLE_GENAI_USE_GCA GEMINI_CLI_TRUST_WORKSPACE
  OPENAI_API_KEY CODEX_API_KEY
  ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
  ANTHROPIC_BASE_URL ANTHROPIC_MODEL ANTHROPIC_SMALL_FAST_MODEL
  ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL
  ANTHROPIC_DEFAULT_HAIKU_MODEL CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC
  ORQ_CLAUDE_CUENTA ORQ_GPT_CUENTA ORQ_GEMINI_CUENTA
  ORQ_ANTIGRAVITY_CUENTA ORQ_MINIMAX_CUENTA
)
declare -ga _ORQ_VARIABLES_POLITICA=(ORQ_AUTO_CHAT ORQ_PERMISOS_TOTALES)
if ! declare -p _ORQ_ORIG_PROVIDER_SET >/dev/null 2>&1; then
  declare -gA _ORQ_ORIG_PROVIDER_SET=() _ORQ_ORIG_PROVIDER_VALUE=()
  for _orq_v in "${_ORQ_VARIABLES_PROVEEDOR[@]}" \
      "${_ORQ_VARIABLES_POLITICA[@]}"; do
    if [ "${!_orq_v+x}" = x ]; then
      _ORQ_ORIG_PROVIDER_SET["$_orq_v"]=1
      _ORQ_ORIG_PROVIDER_VALUE["$_orq_v"]="${!_orq_v}"
    fi
  done
  unset _orq_v
fi
_orq_restaurar_original() {
  local v
  for v in "$@"; do
    unset "$v"
    if [ "${_ORQ_ORIG_PROVIDER_SET[$v]:-}" = 1 ]; then
      printf -v "$v" '%s' "${_ORQ_ORIG_PROVIDER_VALUE[$v]}"
      export "$v"
    fi
  done
}

# Una copia anterior no aporta entorno ni política a la nueva. La política se
# reinicia siempre antes de cargar shell.local.sh: quitar o cambiar una opción
# en ese archivo también debe surtir efecto al volver a cargar la misma copia.
if [ "$_ORQ_CAMBIO_RAIZ" = 1 ]; then
  _orq_restaurar_original "${_ORQ_VARIABLES_PROVEEDOR[@]}"
  unset ORQ_CUENTA _ORQ_ENTORNO_FIRMA
fi
_orq_restaurar_original "${_ORQ_VARIABLES_POLITICA[@]}"

# Preferencias de ESTA maquina, fuera del repositorio. Es un trust anchor
# explícito del operador: se abre una sola vez, se valida por descriptor y se
# ejecuta desde ese descriptor estable. Un symlink, hardlink, otro propietario
# o permisos de escritura de grupo/otros hacen que se ignore sin evaluarlo.
_orq_cargar_config_local() {
  local archivo="$1" contenido rc
  [ -e "$archivo" ] || return 0
  # El lector abre sin bloquear y acumula todos los bytes antes de emitirlos.
  # Por tanto Bash nunca evalua una salida parcial si la validacion falla.
  if ! contenido="$(/usr/bin/python3 -I -S - "$archivo" <<'PY'
import os
import stat
import sys

ruta = sys.argv[1]
if not os.path.isabs(ruta):
    raise SystemExit(1)
partes = ruta.split(os.sep)
if any(parte in (".", "..") for parte in partes):
    raise SystemExit(1)
flags_dir = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
flags_file = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
              | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
fd_dir = os.open(os.path.sep, flags_dir)
try:
    componentes = [parte for parte in partes if parte]
    if not componentes:
        raise SystemExit(1)
    for parte in componentes[:-1]:
        siguiente = os.open(parte, flags_dir, dir_fd=fd_dir)
        os.close(fd_dir)
        fd_dir = siguiente
    fd = os.open(componentes[-1], flags_file, dir_fd=fd_dir)
    try:
        antes = os.fstat(fd)
        if (not stat.S_ISREG(antes.st_mode) or antes.st_uid != os.getuid()
                or antes.st_nlink != 1 or antes.st_mode & 0o022
                or antes.st_size > 64 * 1024):
            raise SystemExit(1)
        bloques = []
        restante = 64 * 1024 + 1
        while restante:
            bloque = os.read(fd, min(16 * 1024, restante))
            if not bloque:
                break
            bloques.append(bloque)
            restante -= len(bloque)
        contenido = b"".join(bloques)
        despues = os.fstat(fd)
        if (len(contenido) > 64 * 1024 or b"\0" in contenido
                or (antes.st_dev, antes.st_ino, antes.st_mtime_ns, antes.st_size)
                != (despues.st_dev, despues.st_ino,
                    despues.st_mtime_ns, despues.st_size)):
            raise SystemExit(1)
        sys.stdout.buffer.write(contenido)
    finally:
        os.close(fd)
finally:
    os.close(fd_dir)
PY
  )"; then
    return 1
  fi
  # ``command substitution`` elimina saltos finales; el here-string agrega
  # uno. Para asignaciones/configuracion shell la semantica se conserva y los
  # bytes ya fueron validados por completo antes de este unico ``source``.
  . /dev/stdin <<<"$contenido"
  rc=$?
  unset contenido
  return "$rc"
}
_ORQ_SHELL_CONFIG="${XDG_CONFIG_HOME:-$_ORQ_USER_HOME/.config}/orquesta/shell.local.sh"
if ! _orq_cargar_config_local "$_ORQ_SHELL_CONFIG"; then
  printf 'orquesta: shell.local.sh inseguro; no se cargo\n' >&2
fi
unset -f _orq_cargar_config_local
# Las preferencias locales controlan autoarranque y permisos, no pueden volver
# a separar el shell del estado cambiando la instalación ya seleccionada.
ORQ_HOME="$_ORQ_HOME_CANONICO"
export ORQ_HOME
_ORQ_RAIZ_ACTIVA="$ORQ_HOME"
unset _ORQ_SHELL_CONFIG _ORQ_HOME_CANONICO

# Fija los comandos interactivos a esta instalación. Así un enlace obsoleto en
# PATH no puede seleccionar otra copia; `command orq` o una ruta B explícita
# quedan igualmente protegidos porque el CLI contrasta su raíz con ORQ_HOME.
# La copia elegida se guarda aparte para que un `export ORQ_HOME=...` accidental
# no cambie código ni estado. Volver a cargar el shell de otra copia actualiza
# deliberadamente ambas variables y las funciones existentes.
unalias orq minimax 2>/dev/null || true
_orq_reafirmar_raiz() {
  ORQ_HOME="$_ORQ_RAIZ_ACTIVA"
  export ORQ_HOME
}
orq() {
  _orq_reafirmar_raiz
  "$_ORQ_RAIZ_ACTIVA/orq" "$@"
}
minimax() {
  _orq_reafirmar_raiz
  "$_ORQ_RAIZ_ACTIVA/tools/minimax" "$@"
}

# Identidad de ESTA terminal: permite medir uso por sesion.
if [ -z "$ORQ_SESION" ]; then
  export ORQ_SESION="$(/usr/bin/date +%Y%m%d-%H%M%S)-$$"
  export ORQ_SESION_TERM="${TERM_PROGRAM:-${KITTY_WINDOW_ID:+kitty}}"
  [ -z "$ORQ_SESION_TERM" ] && ORQ_SESION_TERM="$(/usr/bin/ps -o comm= -p "$PPID" 2>/dev/null)"
  export ORQ_SESION_TERM
fi

_orq_cargar_entorno() {
  _orq_reafirmar_raiz
  local f="$ORQ_HOME/state/entorno.sh"
  [ -f "$f" ] && [ ! -L "$f" ] || return 0
  local m; m=$(/usr/bin/stat -Lc '%d:%i:%s:%Y' "$f" 2>/dev/null) || return 0
  # solo recarga si cambio, y nunca pisa un override manual de esta terminal
  if [ "$m" != "$_ORQ_ENTORNO_FIRMA" ] && [ -z "$ORQ_CUENTA" ]; then
    # El marcador no se ejecuta. Un helper fijo valida profiles.json y entrega
    # pares NUL-delimitados; Bash solo asigna nombres de esta lista cerrada.
    local -a datos=()
    local env_fd env_pid
    exec {env_fd}< <(/usr/bin/python3 -I "$ORQ_HOME/orqenv.py" active)
    env_pid=$!
    mapfile -d '' -t datos <&"$env_fd"
    exec {env_fd}<&-
    wait "$env_pid" || return 1
    [ $((${#datos[@]} % 2)) -eq 0 ] || return 1
    _orq_restaurar_original "${_ORQ_VARIABLES_PROVEEDOR[@]}"
    local i nombre valor
    # Una cuenta activa no puede quedar dominada por credenciales heredadas.
    # Se limpia solo el proveedor que el helper validó; los demás conservan el
    # entorno original de la terminal.
    for ((i=0; i<${#datos[@]}; i+=2)); do
      nombre="${datos[i]}"
      case "$nombre" in
        CLAUDE_CONFIG_DIR)
          unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN \
                ANTHROPIC_BASE_URL ANTHROPIC_MODEL ANTHROPIC_SMALL_FAST_MODEL \
                ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL \
                ANTHROPIC_DEFAULT_HAIKU_MODEL \
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC;;
        CODEX_HOME)
          unset OPENAI_API_KEY CODEX_API_KEY;;
        GEMINI_CLI_HOME)
          unset GEMINI_API_KEY GOOGLE_API_KEY GOOGLE_GENAI_USE_GCA \
                GEMINI_CLI_TRUST_WORKSPACE;;
      esac
    done
    for ((i=0; i<${#datos[@]}; i+=2)); do
      nombre="${datos[i]}"; valor="${datos[i+1]}"
      case "$nombre" in
        CLAUDE_CONFIG_DIR|CODEX_HOME|GEMINI_CLI_HOME|GEMINI_API_KEY|\
        GOOGLE_GENAI_USE_GCA|GEMINI_CLI_TRUST_WORKSPACE|\
        ORQ_CLAUDE_CUENTA|ORQ_GPT_CUENTA|ORQ_GEMINI_CUENTA|\
        ORQ_ANTIGRAVITY_CUENTA)
          printf -v "$nombre" '%s' "$valor"
          export "$nombre"
          ;;
      esac
    done
    unset datos i nombre valor
    _ORQ_ENTORNO_FIRMA="$m"
  fi
}

# Las terminales YA ABIERTAS adoptan la cuenta activa antes de cada comando,
# sin necesidad de reiniciarlas ni de hacer 'source ~/.bashrc'.
case "$PROMPT_COMMAND" in
  *_orq_cargar_entorno*) ;;
  "") PROMPT_COMMAND="_orq_cargar_entorno" ;;
  *)  PROMPT_COMMAND="_orq_cargar_entorno;$PROMPT_COMMAND" ;;
esac

alias orqs='orq status'
alias orqw='orq web'
alias orqu='orq uso'

_orq_restaurar_proveedores() {
  _orq_restaurar_original "${_ORQ_VARIABLES_PROVEEDOR[@]}"
  unset ORQ_CUENTA _ORQ_ENTORNO_FIRMA
  _orq_cargar_entorno
}

# El cambio de raíz ya limpió el entorno antes de cargar la nueva configuración;
# la caché vacía obliga a leer ahora el state de la copia seleccionada.
_orq_cargar_entorno
unset _ORQ_CAMBIO_RAIZ

# Override solo para ESTA terminal (bloquea la recarga automatica)
orquse() {
  _orq_reafirmar_raiz
  local id="$1"
  [ -z "$id" ] && { echo "uso: orquse <id-de-cuenta>"; orq cuentas; return 1; }
  local -a info=()
  local perfil_fd perfil_pid
  exec {perfil_fd}< <(/usr/bin/python3 -I "$_ORQ_RAIZ_ACTIVA/orqenv.py" profile "$id")
  perfil_pid=$!
  mapfile -d '' -t info <&"$perfil_fd"
  exec {perfil_fd}<&-
  wait "$perfil_pid" || { echo "cuenta invalida: $id"; return 1; }
  [ "${#info[@]}" -eq 6 ] || { echo "cuenta desconocida: $id"; return 1; }
  local proveedor="${info[0]}" home="${info[1]}" auth="${info[2]}"
  local clave="${info[3]}" base_url="${info[4]}" modelo="${info[5]}"
  case "$proveedor:$auth:$clave" in
    minimax:*-)
      echo "la cuenta MiniMax no tiene una API key privada"; return 1;;
    gemini:oauth:*) ;;
    gemini:*-)
      echo "la cuenta Gemini no tiene una API key privada"; return 1;;
  esac
  _orq_restaurar_proveedores
  # El override reemplaza únicamente su proveedor. Las cuentas activas de los
  # demás (por ejemplo CODEX_HOME al forzar Claude) continúan coherentes.
  case "$proveedor" in
    claude|minimax)
      unset CLAUDE_CONFIG_DIR ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN \
            CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_MODEL \
            ANTHROPIC_SMALL_FAST_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL \
            ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL \
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC;;
    gpt)
      unset CODEX_HOME OPENAI_API_KEY CODEX_API_KEY;;
    gemini)
      unset GEMINI_CLI_HOME GEMINI_API_KEY GOOGLE_API_KEY \
            GOOGLE_GENAI_USE_GCA GEMINI_CLI_TRUST_WORKSPACE;;
  esac
  case "$proveedor" in
    claude) export CLAUDE_CONFIG_DIR="$home";;
    gpt)    export CODEX_HOME="$home";;
    gemini) export GEMINI_CLI_HOME="$home"; export GEMINI_CLI_TRUST_WORKSPACE=true
            [ "$auth" = "oauth" ] && export GOOGLE_GENAI_USE_GCA=true
            [ "$clave" != "-" ] && export GEMINI_API_KEY="$clave";;
    minimax) # 'claude' en ESTA terminal pasa a hablar con MiniMax
            export CLAUDE_CONFIG_DIR="$home"
            export ANTHROPIC_BASE_URL="$([ "$base_url" != "-" ] && echo "$base_url" || echo "https://api.minimax.io/anthropic")"
            [ "$clave" != "-" ] && export ANTHROPIC_AUTH_TOKEN="$clave"
            local mm; mm="$([ "$modelo" != "-" ] && echo "$modelo" || echo "MiniMax-M3[1m]")"
            export ANTHROPIC_MODEL="$mm" ANTHROPIC_SMALL_FAST_MODEL="$mm" \
                   ANTHROPIC_DEFAULT_OPUS_MODEL="$mm" ANTHROPIC_DEFAULT_SONNET_MODEL="$mm" \
                   ANTHROPIC_DEFAULT_HAIKU_MODEL="$mm"
            export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1;;
    antigravity) ;;
    *) echo "proveedor de cuenta invalido"; return 1;;
  esac
  export ORQ_CUENTA="$id"
  unset info clave
  echo "esta terminal usa ahora: $id ($proveedor)  ·  'orqoff' para volver a Orquesta"
}

orqoff() {
  _orq_restaurar_proveedores
  echo "terminal devuelta a las cuentas activas de Orquesta"
}

orqyo() {
  echo "  sesion  : ${ORQ_SESION}  (${ORQ_SESION_TERM:-?})"
  if [ -n "$ORQ_CUENTA" ]; then
    echo "  override: $ORQ_CUENTA  (fijado a mano en esta terminal)"
  else
    echo "  claude  : ${ORQ_CLAUDE_CUENTA:-(sin activa)}"
    echo "  gpt     : ${ORQ_GPT_CUENTA:-(sin activa)}"
    echo "  gemini  : ${ORQ_ANTIGRAVITY_CUENTA:-${ORQ_GEMINI_CUENTA:-(sin activa)}}"
    echo "  minimax : ${ANTHROPIC_BASE_URL:+esta terminal → $ANTHROPIC_BASE_URL}${ANTHROPIC_BASE_URL:-(usa el comando 'minimax')}"
  fi
}

# ── Permisos locales opcionales
# Solo si ORQ_PERMISOS_TOTALES=1, escribir 'claude', 'codex' o 'agy' a secas
# agrega sus flags sin sandbox. Un clon nuevo no redefine esos comandos.
# Para una llamada puntual sin permisos: 'command claude ...'
if [ "${ORQ_PERMISOS_TOTALES:-0}" = "1" ]; then
  # Respeta tu lanzador claude-vagabond (el del logo de Musashi) si existe,
  # solo le añade los permisos.
  if [ -x "$_ORQ_USER_HOME/.config/kitty/claude-vagabond" ] \
     && [ ! -L "$_ORQ_USER_HOME/.config/kitty/claude-vagabond" ] \
     && [ "$(/usr/bin/stat -Lc '%u:%F' \
          "$_ORQ_USER_HOME/.config/kitty/claude-vagabond" 2>/dev/null)" \
          = "$EUID:regular file" ]; then
    claude() {
      if [ "${ORQ_PERMISOS_TOTALES:-0}" != 1 ]; then command claude "$@"; return; fi
      "$_ORQ_USER_HOME/.config/kitty/claude-vagabond" --dangerously-skip-permissions "$@"
    }
  else
    claude() {
      if [ "${ORQ_PERMISOS_TOTALES:-0}" != 1 ]; then command claude "$@"; return; fi
      command claude --dangerously-skip-permissions "$@"
    }
  fi
  codex()  {
    if [ "${ORQ_PERMISOS_TOTALES:-0}" != 1 ]; then command codex "$@"; return; fi
    case "$1" in
      exec|login|logout|mcp|sandbox|apply|resume)
        command codex "$@";;
      *) command codex --dangerously-bypass-approvals-and-sandbox "$@";;
    esac
  }
  agy()    {
    if [ "${ORQ_PERMISOS_TOTALES:-0}" != 1 ]; then command agy "$@"; return; fi
    command agy --dangerously-skip-permissions "$@"
  }
  gemini() {
    if [ "${ORQ_PERMISOS_TOTALES:-0}" != 1 ]; then command gemini "$@"; return; fi
    command gemini --yolo "$@"
  }
fi

# ── Un prompt, todas las IA ─────────────────────────────────────────────
# 'ia "lo que sea"'            -> lo delega a la mejor cuenta para esa tarea
# 'ia -p "haz un bot ..."'     -> proyecto completo repartido entre todas
ia() {
  if [ "$1" = "-p" ] || [ "$1" = "--proyecto" ]; then
    shift; orq proyecto "$@"
  else
    orq ask "$@"
  fi
}

# ── Autoarranque de chat: local, opt-in y por terminal
# El chat manual siempre esta disponible con `orq chat`. El autoarranque solo
# se habilita en la configuracion local, nunca al clonar el repositorio:
# ORQ_AUTO_CHAT=1              cualquier terminal interactiva
# ORQ_AUTO_CHAT=kitty,wezterm  solo identificadores de esta lista
# ORQ_AUTO_CHAT=0 (default)    nunca
_orq_terminal_id() {
  if [ -n "${TERM_PROGRAM:-}" ]; then
    printf '%s' "$TERM_PROGRAM" | /usr/bin/tr '[:upper:]' '[:lower:]'
  elif [ -n "${KITTY_WINDOW_ID:-}" ]; then
    printf '%s' kitty
  elif [ -n "${WEZTERM_PANE:-}" ]; then
    printf '%s' wezterm
  elif [ -n "${ALACRITTY_WINDOW_ID:-}" ]; then
    printf '%s' alacritty
  else
    /usr/bin/ps -o comm= -p "$PPID" 2>/dev/null \
      | /usr/bin/sed 's|.*/||; s/[[:space:]]//g' \
      | /usr/bin/tr '[:upper:]' '[:lower:]'
  fi
}

_orq_auto_chat_habilitado() {
  local politica actual lista
  politica="${ORQ_AUTO_CHAT:-0}"
  case "$politica" in
    1|true|TRUE|yes|YES|always|all) return 0 ;;
    0|false|FALSE|no|NO|off|"") return 1 ;;
  esac
  actual="$(_orq_terminal_id)"
  lista=",$(printf '%s' "$politica" | /usr/bin/tr '[:upper:] ' '[:lower:],'),"
  case "$lista" in *",$actual,"*) return 0 ;; *) return 1 ;; esac
}

if [ "${TERM:-dumb}" != "dumb" ] && [ -t 0 ] && [ -t 1 ] \
   && [ -z "${ORQ_SIN_MODO_PROMPT:-}" ] && [ -z "${ORQ_CHAT_ACTIVO:-}" ] \
   && _orq_auto_chat_habilitado; then
  if [ -x "$ORQ_HOME/orq" ]; then
    _ORQ_MODO_ACTUAL="$(_orq_terminal_id)"
    ORQ_MODO="$_ORQ_MODO_ACTUAL" ORQ_CHAT_ACTIVO=1 "$ORQ_HOME/orq" chat
    # Red de seguridad: si el chat falla, el shell sigue disponible.
    if [ $? -ne 0 ]; then
      printf '\033[38;2;224;50;46m▍\033[0m la interfaz fallo; tienes el shell normal.\n'
      printf '  \033[2mreintenta con:  orq chat\033[0m\n'
    fi
    unset _ORQ_MODO_ACTUAL
  fi
fi

unset -f _orq_terminal_id _orq_auto_chat_habilitado
