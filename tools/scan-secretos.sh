#!/usr/bin/env bash
# Bloquea el commit si detecta credenciales. Uso: tools/scan-secretos.sh [--staged]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
FALLO=0
rojo() { printf '\033[31m%s\033[0m\n' "$*"; }
verde(){ printf '\033[32m%s\033[0m\n' "$*"; }

ARCHIVOS=()
if [ "${1:-}" = "--staged" ]; then
  LISTA=$(mktemp) || { rojo "no pude crear una lista temporal"; exit 2; }
  trap 'rm -f -- "$LISTA"' EXIT
  if ! git diff --cached --no-renames --name-only --diff-filter=ACMRTUXB -z >"$LISTA"; then
    rojo "no pude leer el indice de Git; se bloquea por seguridad"
    exit 2
  fi
  mapfile -d '' -t ARCHIVOS <"$LISTA"
  ORIGEN=indice
else
  LISTA=$(mktemp) || { rojo "no pude crear una lista temporal"; exit 2; }
  trap 'rm -f -- "$LISTA"' EXIT
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if ! git ls-files -z >"$LISTA"; then
      rojo "no pude enumerar los archivos de Git; se bloquea por seguridad"
      exit 2
    fi
  else
    find . -type f -not -path './.git/*' -printf '%P\0' >"$LISTA"
  fi
  mapfile -d '' -t ARCHIVOS <"$LISTA"
  ORIGEN=disco
fi
[ "${#ARCHIVOS[@]}" -eq 0 ] && { verde "nada que revisar"; exit 0; }

leer_archivo() {
  if [ "$ORIGEN" = indice ]; then
    git show ":$1" 2>/dev/null
  else
    command cat -- "$1" 2>/dev/null
  fi
}

comprobar_lectura() {
  if [ "$ORIGEN" = indice ]; then
    git cat-file -e ":$1" 2>/dev/null
  else
    [ -r "$1" ]
  fi
}

# 1) rutas que jamas deben estar versionadas
for f in "${ARCHIVOS[@]}"; do
  ruta=${f,,}
  case "/$ruta" in
    */accounts/*|*/state/*|*/profiles.json|*/.env*|*credential*|*auth.json|*api_key*|*.pem|*.key)
      rojo "BLOQUEADO · archivo sensible en el commit: $f"; FALLO=1;;
  esac
done

# 2) patrones de credencial dentro del contenido
PATRONES='sk-ant-[A-Za-z0-9_-]{20,}|sk-(proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{30,}|ya29\.[0-9A-Za-z_-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.'
for f in "${ARCHIVOS[@]}"; do
  if ! comprobar_lectura "$f"; then
    rojo "BLOQUEADO · no pude leer: $f"; FALLO=1; continue
  fi
  linea=$(leer_archivo "$f" | grep -aEn "$PATRONES" 2>/dev/null | sed -n '1{s/:.*//;p;}')
  if [ -n "$linea" ]; then
    rojo "BLOQUEADO · posible credencial en: $f (linea $linea)"
    FALLO=1
  fi
done

# 3) asignaciones sospechosas con valor literal
ASIGNACION='(password|passwd|contrasena|contraseña|secret|api[_-]?key|token)[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']{8,}'
for f in "${ARCHIVOS[@]}"; do
  if ! comprobar_lectura "$f"; then
    [ "$FALLO" -eq 1 ] || rojo "BLOQUEADO · no pude leer: $f"
    FALLO=1; continue
  fi
  linea=$(leer_archivo "$f" | grep -aEn "$ASIGNACION" 2>/dev/null | sed -n '1{s/:.*//;p;}')
  if [ -n "$linea" ]; then
    rojo "REVISAR · asignacion sospechosa en: $f (linea $linea)"
    FALLO=1
  fi
done

[ $FALLO -eq 0 ] && { verde "limpio · ninguna credencial detectada"; exit 0; }
rojo "ABORTAR: corrige lo anterior antes de subir."; exit 1
