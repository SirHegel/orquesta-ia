# --- Orquesta IA · se carga en cada terminal desde ~/.bashrc y ~/.profile ---
export ORQ_HOME="$HOME/Documentos/ia/orquesta"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH";; esac

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
    . "$f"
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

# Override solo para ESTA terminal (bloquea la recarga automatica)
orquse() {
  local id="$1"
  [ -z "$id" ] && { echo "uso: orquse <id-de-cuenta>"; orq cuentas; return 1; }
  local info
  info=$(python3 - "$id" <<'PY'
import json,sys,os
b=os.environ.get("ORQ_HOME") or os.path.expanduser("~/Documentos/ia/orquesta")
p=json.load(open(os.path.join(b,"profiles.json")))["profiles"].get(sys.argv[1])
if not p: sys.exit(1)
h=p.get("home") or os.path.join(b,"accounts",sys.argv[1])
print(p["provider"], os.path.expanduser(h), p.get("auth",""), p.get("api_key_file",""))
PY
) || { echo "cuenta desconocida: $id"; return 1; }
  set -- $info
  case "$1" in
    claude) export CLAUDE_CONFIG_DIR="$2";;
    gpt)    export CODEX_HOME="$2";;
    gemini) export GEMINI_CLI_HOME="$2"; export GEMINI_CLI_TRUST_WORKSPACE=true
            [ "$3" = "oauth" ] && export GOOGLE_GENAI_USE_GCA=true
            [ -n "$4" ] && [ -f "$4" ] && export GEMINI_API_KEY="$(cat "$4")";;
  esac
  export ORQ_CUENTA="$id"
  echo "esta terminal usa ahora: $id ($1)  ·  'orqoff' para volver a Orquesta"
}

orqoff() {
  unset CLAUDE_CONFIG_DIR CODEX_HOME GEMINI_CLI_HOME GEMINI_API_KEY \
        GOOGLE_GENAI_USE_GCA GEMINI_CLI_TRUST_WORKSPACE ORQ_CUENTA _ORQ_ENTORNO_MTIME
  _orq_cargar_entorno
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
  fi
}

# ── Permisos siempre dados ──────────────────────────────────────────────
# Escribir 'claude', 'codex' o 'agy' a secas ya trae los flags. Sin esto
# tendrias que acordarte del flag cada vez (antes hacias 'codex --yolo').
# Para una llamada puntual sin permisos: 'command claude ...'
if [ "${ORQ_PERMISOS_TOTALES:-1}" = "1" ]; then
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

# ── kitty = interfaz de lenguaje natural ────────────────────────────────
# Kitty no es un shell con IA encima: arranca directamente la conversacion.
# Dentro tienes /shell para comandos del sistema, y /salir para volver a bash.
# Para abrir kitty como bash normal:  ORQ_SIN_MODO_PROMPT=1 kitty
if [ -n "$KITTY_WINDOW_ID" ] && [ "$TERM" != "dumb" ] \
   && [ -z "$ORQ_SIN_MODO_PROMPT" ] && [ -z "$ORQ_CHAT_ACTIVO" ]; then
  export ORQ_MODO=kitty ORQ_CHAT_ACTIVO=1
  if [ -f "$ORQ_HOME/orqchat.py" ]; then
    python3 "$ORQ_HOME/orqchat.py"
    # red de seguridad: si el chat falla, no te quedas sin terminal
    if [ $? -ne 0 ]; then
      printf '\033[38;2;224;50;46m▍\033[0m la interfaz fallo; tienes bash normal.\n'
      printf '  \033[2mreintenta con:  orq chat\033[0m\n'
    fi
  fi
fi
