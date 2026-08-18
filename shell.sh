# --- Orquesta IA (cargado desde ~/.bashrc) ---
export ORQ_HOME="$HOME/Documentos/ia/orquesta"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH";; esac

# atajos
alias orqs='orq status'
alias orqw='orq web'
# usar una cuenta concreta en ESTA terminal (claude/codex nativos, no via orq)
orquse() {
  local id="$1"
  [ -z "$id" ] && { echo "uso: orquse <id-de-cuenta>"; orq cuentas; return 1; }
  local info
  info=$(python3 - "$id" <<'PY'
import json,sys,os
p=json.load(open(os.path.expanduser("~/Documentos/ia/orquesta/profiles.json")))["profiles"].get(sys.argv[1])
if not p: sys.exit(1)
print(p["provider"], os.path.expanduser(p.get("home") or ""))
PY
) || { echo "cuenta desconocida: $id"; return 1; }
  set -- $info
  case "$1" in
    claude) export CLAUDE_CONFIG_DIR="$2"; unset CODEX_HOME;;
    gpt)    export CODEX_HOME="$2"; unset CLAUDE_CONFIG_DIR;;
    gemini) export GEMINI_CONFIG_DIR="$2"
            [ -f "$2/api_key" ] && export GEMINI_API_KEY="$(cat "$2/api_key")";;
  esac
  export ORQ_CUENTA="$id"
  echo "esta terminal ahora usa: $id ($1)"
}
orqoff() { unset CLAUDE_CONFIG_DIR CODEX_HOME GEMINI_CONFIG_DIR GEMINI_API_KEY ORQ_CUENTA
           echo "terminal devuelta a las cuentas por defecto"; }
