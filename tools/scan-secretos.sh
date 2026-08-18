#!/usr/bin/env bash
# Bloquea el commit si detecta credenciales. Uso: tools/scan-secretos.sh [--staged]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
FALLO=0
rojo() { printf '\033[31m%s\033[0m\n' "$*"; }
verde(){ printf '\033[32m%s\033[0m\n' "$*"; }

if [ "${1:-}" = "--staged" ]; then
  ARCHIVOS=$(git diff --cached --name-only --diff-filter=ACM)
else
  ARCHIVOS=$(git ls-files 2>/dev/null || find . -type f -not -path './.git/*' -printf '%P\n')
fi
[ -z "$ARCHIVOS" ] && { verde "nada que revisar"; exit 0; }

# 1) rutas que jamas deben estar versionadas
while IFS= read -r f; do
  case "$f" in
    accounts/*|state/*|profiles.json|*.credentials.json|*auth.json|*api_key|*.pem|*.key|.env*)
      rojo "BLOQUEADO · archivo sensible en el commit: $f"; FALLO=1;;
  esac
done <<< "$ARCHIVOS"

# 2) patrones de credencial dentro del contenido
PATRONES='sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|AIza[0-9A-Za-z_-]{30,}|ya29\.[0-9A-Za-z_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.'
while IFS= read -r f; do
  [ -f "$f" ] || continue
  case "$f" in tools/scan-secretos.sh|SECURITY.md) continue;; esac
  if grep -EnI --binary-files=without-match "$PATRONES" "$f" >/dev/null 2>&1; then
    rojo "BLOQUEADO · posible credencial en: $f"
    grep -EnI --binary-files=without-match "$PATRONES" "$f" | head -3 | cut -c1-100
    FALLO=1
  fi
done <<< "$ARCHIVOS"

# 3) asignaciones sospechosas con valor literal
while IFS= read -r f; do
  [ -f "$f" ] || continue
  case "$f" in tools/scan-secretos.sh|SECURITY.md|*.md) continue;; esac
  if grep -EnI --binary-files=without-match \
     '(password|passwd|contrasena|contraseña|secret|api[_-]?key|token)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{8,}' \
     "$f" >/dev/null 2>&1; then
    rojo "REVISAR · asignacion sospechosa en: $f"
    grep -EnI '(password|passwd|contrasena|contraseña|secret|api[_-]?key|token)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{8,}' "$f" | head -3 | cut -c1-100
    FALLO=1
  fi
done <<< "$ARCHIVOS"

[ $FALLO -eq 0 ] && { verde "limpio · ninguna credencial detectada"; exit 0; }
rojo "ABORTAR: corrige lo anterior antes de subir."; exit 1
