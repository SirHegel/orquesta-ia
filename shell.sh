# --- Orquesta IA · integracion opcional para shells Bash interactivos ---
# `orq chat` no necesita este archivo. Si se carga desde ~/.bashrc, no debe
# arrancar nada por defecto ni intervenir en shells no interactivos.
case $- in
  *i*) ;;
  *) return 0 2>/dev/null || exit 0 ;;
esac

# La ubicacion del clon no es fija. ORQ_HOME sigue admitiendo un override.
if [ -z "${ORQ_HOME:-}" ]; then
  _ORQ_SHELL_FILE="${BASH_SOURCE[0]:-}"
  if [ -n "$_ORQ_SHELL_FILE" ]; then
    ORQ_HOME="$(CDPATH= cd -- "$(dirname -- "$_ORQ_SHELL_FILE")" 2>/dev/null && pwd -P)"
  fi
  [ -n "${ORQ_HOME:-}" ] || ORQ_HOME="$HOME/.local/share/orquesta"
  export ORQ_HOME
  unset _ORQ_SHELL_FILE
fi
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH";; esac

# Preferencias de ESTA maquina, fuera del repositorio. Es codigo de shell local:
# debe ser propiedad del usuario y no se copia ni se publica con el proyecto.
_ORQ_SHELL_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/orquesta/shell.local.sh"
if [ -r "$_ORQ_SHELL_CONFIG" ]; then
  . "$_ORQ_SHELL_CONFIG"
fi
unset _ORQ_SHELL_CONFIG

# Foto del entorno anterior a Orquesta. Permite cambiar MiniMax -> Claude sin
# filtrar endpoint/modelo y, con `orqoff`, restaurar valores que ya eran del
# usuario en vez de borrarlos definitivamente.
if ! declare -p _ORQ_ORIG_PROVIDER_SET >/dev/null 2>&1; then
  declare -gA _ORQ_ORIG_PROVIDER_SET=() _ORQ_ORIG_PROVIDER_VALUE=()
  for _orq_v in CLAUDE_CONFIG_DIR CODEX_HOME GEMINI_CLI_HOME GEMINI_API_KEY \
      GOOGLE_API_KEY GOOGLE_GENAI_USE_GCA GEMINI_CLI_TRUST_WORKSPACE \
      ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN \
      ANTHROPIC_BASE_URL ANTHROPIC_MODEL ANTHROPIC_SMALL_FAST_MODEL \
      ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL \
      ANTHROPIC_DEFAULT_HAIKU_MODEL CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC; do
    if [ "${!_orq_v+x}" = x ]; then
      _ORQ_ORIG_PROVIDER_SET["$_orq_v"]=1
      _ORQ_ORIG_PROVIDER_VALUE["$_orq_v"]="${!_orq_v}"
    fi
  done
  unset _orq_v
fi

# Identidad de ESTA terminal: permite medir uso por sesion.
if [ -z "$ORQ_SESION" ]; then
  export ORQ_SESION="$(date +%Y%m%d-%H%M%S)-$$"
  export ORQ_SESION_TERM="${TERM_PROGRAM:-${KITTY_WINDOW_ID:+kitty}}"
  [ -z "$ORQ_SESION_TERM" ] && ORQ_SESION_TERM="$(ps -o comm= -p "$PPID" 2>/dev/null)"
  export ORQ_SESION_TERM
fi

_orq_cargar_entorno() {
  local f="$ORQ_HOME/state/entorno.sh"
  [ -f "$f" ] || return 0
  local m; m=$(stat -c %Y "$f" 2>/dev/null) || return 0
  # solo recarga si cambio, y nunca pisa un override manual de esta terminal
  if [ "$m" != "$_ORQ_ENTORNO_MTIME" ] && [ -z "$ORQ_CUENTA" ]; then
    # Los perfiles eligen cuentas, no la politica de sandbox de esta maquina.
    # Conserva el valor del entorno/config local e ignora el legado del state.
    local permisos_set="${ORQ_PERMISOS_TOTALES+x}"
    local permisos_val="${ORQ_PERMISOS_TOTALES:-}"
    . "$f"
    if [ "$permisos_set" = x ]; then
      ORQ_PERMISOS_TOTALES="$permisos_val"
    else
      unset ORQ_PERMISOS_TOTALES
    fi
    _ORQ_ENTORNO_MTIME="$m"
  fi
}
_orq_cargar_entorno

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
  local v
  for v in CLAUDE_CONFIG_DIR CODEX_HOME GEMINI_CLI_HOME GEMINI_API_KEY \
      GOOGLE_API_KEY GOOGLE_GENAI_USE_GCA GEMINI_CLI_TRUST_WORKSPACE \
      ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN \
      ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN ANTHROPIC_MODEL ANTHROPIC_SMALL_FAST_MODEL \
      ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL \
      ANTHROPIC_DEFAULT_HAIKU_MODEL CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC; do
    unset "$v"
    if [ "${_ORQ_ORIG_PROVIDER_SET[$v]:-}" = 1 ]; then
      printf -v "$v" '%s' "${_ORQ_ORIG_PROVIDER_VALUE[$v]}"
      export "$v"
    fi
  done
  unset ORQ_CUENTA _ORQ_ENTORNO_MTIME
  _orq_cargar_entorno
}

# Override solo para ESTA terminal (bloquea la recarga automatica)
orquse() {
  local id="$1"
  [ -z "$id" ] && { echo "uso: orquse <id-de-cuenta>"; orq cuentas; return 1; }
  local -a info=()
  mapfile -t info < <(python3 - "$id" <<'PY'
import json,sys,os
b=os.environ.get("ORQ_HOME") or os.path.expanduser("~/Documentos/ia/orquesta")
p=json.load(open(os.path.join(b,"profiles.json")))["profiles"].get(sys.argv[1])
if not p: sys.exit(1)
h=p.get("home") or os.path.join(b,"accounts",sys.argv[1])
def ruta(v):
    if not v: return "-"
    v=os.path.expanduser(v)
    return os.path.abspath(v if os.path.isabs(v) else os.path.join(b,v))
if not os.path.isabs(os.path.expanduser(h)):
    h=os.path.join(b,os.path.expanduser(h))
for valor in (p["provider"], os.path.abspath(os.path.expanduser(h)),
              p.get("auth") or "-", ruta(p.get("api_key_file")),
              p.get("base_url") or "-", p.get("model") or "-"):
    print(valor)
PY
)
  [ "${#info[@]}" -eq 6 ] || { echo "cuenta desconocida: $id"; return 1; }
  _orq_restaurar_proveedores
  local proveedor="${info[0]}" home="${info[1]}" auth="${info[2]}"
  local key_file="${info[3]}" base_url="${info[4]}" modelo="${info[5]}"
  case "$proveedor" in
    claude) export CLAUDE_CONFIG_DIR="$home";;
    gpt)    export CODEX_HOME="$home";;
    gemini) export GEMINI_CLI_HOME="$home"; export GEMINI_CLI_TRUST_WORKSPACE=true
            [ "$auth" = "oauth" ] && export GOOGLE_GENAI_USE_GCA=true
            [ "$key_file" != "-" ] && [ -f "$key_file" ] && export GEMINI_API_KEY="$(cat "$key_file")";;
    minimax) # 'claude' en ESTA terminal pasa a hablar con MiniMax
            export CLAUDE_CONFIG_DIR="$home"
            unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN
            export ANTHROPIC_BASE_URL="$([ "$base_url" != "-" ] && echo "$base_url" || echo "https://api.minimax.io/anthropic")"
            [ "$key_file" != "-" ] && [ -f "$key_file" ] && export ANTHROPIC_AUTH_TOKEN="$(cat "$key_file")"
            local mm; mm="$([ "$modelo" != "-" ] && echo "$modelo" || echo "MiniMax-M3[1m]")"
            export ANTHROPIC_MODEL="$mm" ANTHROPIC_SMALL_FAST_MODEL="$mm" \
                   ANTHROPIC_DEFAULT_OPUS_MODEL="$mm" ANTHROPIC_DEFAULT_SONNET_MODEL="$mm" \
                   ANTHROPIC_DEFAULT_HAIKU_MODEL="$mm"
            export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1;;
  esac
  export ORQ_CUENTA="$id"
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
  if [ -x "$HOME/.config/kitty/claude-vagabond" ]; then
    claude() { "$HOME/.config/kitty/claude-vagabond" --dangerously-skip-permissions "$@"; }
  else
    claude() { command claude --dangerously-skip-permissions "$@"; }
  fi
  codex()  {
    case "$1" in
      exec|login|logout|mcp|sandbox|apply|resume)
        command codex "$@";;
      *) command codex --dangerously-bypass-approvals-and-sandbox "$@";;
    esac
  }
  agy()    { command agy --dangerously-skip-permissions "$@"; }
  gemini() { command gemini --yolo "$@"; }
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
    printf '%s' "$TERM_PROGRAM" | tr '[:upper:]' '[:lower:]'
  elif [ -n "${KITTY_WINDOW_ID:-}" ]; then
    printf '%s' kitty
  elif [ -n "${WEZTERM_PANE:-}" ]; then
    printf '%s' wezterm
  elif [ -n "${ALACRITTY_WINDOW_ID:-}" ]; then
    printf '%s' alacritty
  else
    ps -o comm= -p "$PPID" 2>/dev/null | sed 's|.*/||; s/[[:space:]]//g' | tr '[:upper:]' '[:lower:]'
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
  lista=",$(printf '%s' "$politica" | tr '[:upper:] ' '[:lower:],'),"
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
