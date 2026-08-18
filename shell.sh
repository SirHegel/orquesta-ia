# --- Orquesta IA · se carga en cada terminal desde ~/.bashrc ---
export ORQ_HOME="$HOME/Documentos/ia/orquesta"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH";; esac

# Cuenta activa de cada proveedor, fijada con 'orq usar'.
# Gracias a esto, escribir 'claude' o 'codex' en CUALQUIER terminal usa
# la cuenta correcta sin volver a autenticar nada.
[ -f "$ORQ_HOME/state/entorno.sh" ] && . "$ORQ_HOME/state/entorno.sh"

alias orqs='orq status'
alias orqw='orq web'

# Override solo para ESTA terminal (no cambia las demás)
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
  echo "esta terminal usa ahora: $id ($1)"
}

# Volver a las cuentas activas de Orquesta
orqoff() {
  unset CLAUDE_CONFIG_DIR CODEX_HOME GEMINI_CLI_HOME GEMINI_API_KEY \
        GOOGLE_GENAI_USE_GCA GEMINI_CLI_TRUST_WORKSPACE ORQ_CUENTA
  [ -f "$ORQ_HOME/state/entorno.sh" ] && . "$ORQ_HOME/state/entorno.sh"
  echo "terminal devuelta a las cuentas activas de Orquesta"
}

# Qué cuenta está usando esta terminal ahora mismo
orqyo() {
  echo "  claude : ${ORQ_CUENTA:-${ORQ_CLAUDE_CUENTA:-(por defecto del sistema)}}"
  echo "  gpt    : ${ORQ_CUENTA:-${ORQ_GPT_CUENTA:-(por defecto del sistema)}}"
  echo "  gemini : ${ORQ_CUENTA:-${ORQ_GEMINI_CUENTA:-(sin configurar)}}"
}
