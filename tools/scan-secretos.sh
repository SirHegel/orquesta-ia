#!/usr/bin/bash
# Bloquea el commit si detecta credenciales.
# Uso: scan-secretos.sh [--staged|--todo] [--repo /ruta/al/repo]
set -uo pipefail
PATH=/usr/bin:/bin
export PATH
GIT_SEGURO=(
  /usr/bin/git --no-pager --no-replace-objects
  -c core.hooksPath=/dev/null
  -c core.fsmonitor=false
  -c core.untrackedCache=false
  -c core.commitGraph=false
  -c fetch.writeCommitGraph=false
  -c diff.external=
  -c credential.helper=
  -c protocol.ext.allow=never
  -c advice.graftFileDeprecated=false
)
git_seguro() {
  env -i HOME=/nonexistent PATH=/usr/bin:/bin LC_ALL=C.UTF-8 \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_OPTIONAL_LOCKS=0 \
    GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 \
    GIT_SHALLOW_FILE=/dev/null GIT_GRAFT_FILE=/dev/null \
    "${GIT_SEGURO[@]}" "$@"
}
MODO=disco
REPO=""
REF=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --staged) MODO=indice ;;
    --todo) MODO=todo ;;
    --commit)
      shift
      [ "$#" -gt 0 ] || { echo "falta el hash para --commit" >&2; exit 2; }
      MODO=commit
      REF="$1" ;;
    --repo)
      shift
      [ "$#" -gt 0 ] || { echo "falta la ruta para --repo" >&2; exit 2; }
      REPO="$1" ;;
    *) echo "opcion desconocida: $1" >&2; exit 2 ;;
  esac
  shift
done
[ "$MODO" != commit ] || [[ "$REF" =~ ^[0-9a-fA-F]{7,64}$ ]] || {
  echo "hash invalido para --commit" >&2; exit 2;
}
[ -n "$REPO" ] || REPO="$(dirname "$0")/.."
cd "$REPO" || exit 2
FALLO=0
rojo() { printf '\033[31m%s\033[0m\n' "$*"; }
verde(){ printf '\033[32m%s\033[0m\n' "$*"; }
ruta_visible() {
  LC_ALL=C printf '%s' "$1" | /usr/bin/tr -d '[:cntrl:]' | /usr/bin/cut -c1-240
}

ARCHIVOS=()
METADATA=""
if [ "$MODO" = indice ]; then
  LISTA=$(mktemp) || { rojo "no pude crear una lista temporal"; exit 2; }
  trap 'rm -f -- "$LISTA"' EXIT
  if ! git_seguro diff --no-ext-diff --no-textconv --cached --no-renames \
      --name-only --diff-filter=ACMRTUXB -z >"$LISTA"; then
    rojo "no pude leer el indice de Git; se bloquea por seguridad"
    exit 2
  fi
  ORIGEN=indice
elif [ "$MODO" = commit ]; then
  LISTA=$(mktemp) || { rojo "no pude crear una lista temporal"; exit 2; }
  METADATA=$(mktemp) || {
    /usr/bin/rm -f -- "$LISTA"
    rojo "no pude crear metadata temporal"; exit 2;
  }
  trap '/usr/bin/rm -f -- "$LISTA" "$METADATA"' EXIT
  TIPO=$(git_seguro cat-file -t "$REF" 2>/dev/null) || {
    rojo "no pude resolver el tipo del objeto; se bloquea por seguridad"; exit 2;
  }
  [ "$TIPO" = commit ] || {
    rojo "el objeto solicitado no es un commit"; exit 2;
  }
  TAM_METADATA=$(git_seguro cat-file -s "$REF" 2>/dev/null) \
    && [[ "$TAM_METADATA" =~ ^[0-9]+$ ]] \
    && [ "$TAM_METADATA" -le $((1024 * 1024)) ] || {
      rojo "metadata de commit invalida o demasiado grande"; exit 2;
    }
  if ! git_seguro cat-file commit "$REF" >"$METADATA" 2>/dev/null; then
    rojo "no pude leer la metadata del commit; se bloquea por seguridad"
    exit 2
  fi
  TAM_LEIDO=$(/usr/bin/stat -c %s "$METADATA" 2>/dev/null) || {
    rojo "no pude medir la metadata del commit"; exit 2;
  }
  [ "$TAM_LEIDO" -eq "$TAM_METADATA" ] || {
    rojo "la metadata del commit cambio durante la lectura"; exit 2;
  }
  if ! git_seguro ls-tree -r --name-only -z "$REF" >"$LISTA"; then
    rojo "no pude leer el commit; se bloquea por seguridad"
    exit 2
  fi
  ORIGEN=commit
else
  LISTA=$(mktemp) || { rojo "no pude crear una lista temporal"; exit 2; }
  trap 'rm -f -- "$LISTA"' EXIT
  if git_seguro rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if [ "$MODO" = todo ]; then
      GIT_LISTA=(git_seguro ls-files --cached --others --exclude-standard -z)
    else
      GIT_LISTA=(git_seguro ls-files -z)
    fi
    if ! "${GIT_LISTA[@]}" >"$LISTA"; then
      rojo "no pude enumerar los archivos de Git; se bloquea por seguridad"
      exit 2
    fi
  else
    find . -type f -not -path './.git/*' -printf '%P\0' >"$LISTA"
  fi
  ORIGEN=disco
fi
TAM_LISTA=$(/usr/bin/stat -c %s "$LISTA" 2>/dev/null) || {
  rojo "no pude medir la lista de archivos; se bloquea por seguridad"; exit 2;
}
[ "$TAM_LISTA" -le $((16 * 1024 * 1024)) ] || {
  rojo "demasiadas rutas para revisar con seguridad"; exit 2;
}
mapfile -d '' -t ARCHIVOS <"$LISTA"
[ "${#ARCHIVOS[@]}" -le 100000 ] || {
  rojo "demasiados archivos para revisar con seguridad"; exit 2;
}

ruta_ausente_segura() {
  /usr/bin/python3 -I -S - "$1" <<'PY'
import os
import sys

relativa = sys.argv[1]
partes = relativa.split("/")
if (not relativa or os.path.isabs(relativa)
        or any(x in ("", ".", "..") for x in partes)):
    raise SystemExit(1)
flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) \
    | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
fd = os.open(".", flags)
try:
    for parte in partes[:-1]:
        try:
            siguiente = os.open(parte, flags, dir_fd=fd)
        except FileNotFoundError:
            raise SystemExit(0)
        except OSError:
            raise SystemExit(1)
        os.close(fd)
        fd = siguiente
    try:
        os.stat(partes[-1], dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        raise SystemExit(0)
    except OSError:
        raise SystemExit(1)
finally:
    os.close(fd)
raise SystemExit(1)
PY
}

# Antes de ``git add -A``, --todo ve también paths cached que ya fueron
# borrados. Solo omitimos una ausencia confirmada por openat/O_NOFOLLOW y que
# el índice real reconoce como tracked; enlaces, FIFO y errores siguen en la
# lista para que la lectura posterior bloquee.
if [ "$MODO" = todo ] && [ "$ORIGEN" = disco ]; then
  FILTRADOS=()
  for f in "${ARCHIVOS[@]}"; do
    if ruta_ausente_segura "$f" \
        && git_seguro ls-files --error-unmatch -- "$f" >/dev/null 2>&1; then
      continue
    fi
    FILTRADOS+=("$f")
  done
  ARCHIVOS=("${FILTRADOS[@]}")
fi
if [ "${#ARCHIVOS[@]}" -eq 0 ] && [ "$MODO" != commit ]; then
  verde "nada que revisar"; exit 0
fi

leer_disco_seguro() {
  /usr/bin/python3 -I -S - "$1" <<'PY'
import os
import stat
import sys

relativa = sys.argv[1]
partes = relativa.split("/")
if (not relativa or os.path.isabs(relativa)
        or any(x in ("", ".", "..") for x in partes)):
    raise SystemExit(1)
flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) \
    | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
flags_file = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) \
    | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
fd = os.open(".", flags_dir)
try:
    for parte in partes[:-1]:
        siguiente = os.open(parte, flags_dir, dir_fd=fd)
        os.close(fd)
        fd = siguiente
    archivo = os.open(partes[-1], flags_file, dir_fd=fd)
    try:
        estado = os.fstat(archivo)
        if (not stat.S_ISREG(estado.st_mode) or estado.st_uid != os.getuid()
                or estado.st_nlink != 1 or estado.st_size > 8 * 1024 * 1024):
            raise SystemExit(1)
        while True:
            bloque = os.read(archivo, 64 * 1024)
            if not bloque:
                break
            sys.stdout.buffer.write(bloque)
    finally:
        os.close(archivo)
finally:
    os.close(fd)
PY
}

leer_archivo() {
  if [ "$ORIGEN" = indice ]; then
    git_seguro show ":$1" 2>/dev/null
  elif [ "$ORIGEN" = commit ]; then
    git_seguro show "$REF:$1" 2>/dev/null
  else
    leer_disco_seguro "$1" 2>/dev/null
  fi
}

comprobar_lectura() {
  if [ "$ORIGEN" = indice ]; then
    tam=$(git_seguro cat-file -s ":$1" 2>/dev/null) \
      && [[ "$tam" =~ ^[0-9]+$ ]] && [ "$tam" -le $((8 * 1024 * 1024)) ]
  elif [ "$ORIGEN" = commit ]; then
    tam=$(git_seguro cat-file -s "$REF:$1" 2>/dev/null) \
      && [[ "$tam" =~ ^[0-9]+$ ]] && [ "$tam" -le $((8 * 1024 * 1024)) ]
  else
    leer_disco_seguro "$1" >/dev/null 2>&1
  fi
}

# 1) rutas que jamas deben estar versionadas
for f in "${ARCHIVOS[@]}"; do
  ruta=${f,,}
  case "/$ruta" in
    */accounts/*|*/state/*|*/profiles.json|*/.env*|*credential*|*auth.json|*api_key*|*.pem|*.key)
      rojo "BLOQUEADO · archivo sensible en el commit: $(ruta_visible "$f")"; FALLO=1;;
  esac
done

# 2) patrones de credencial dentro del contenido
PATRONES='sk-ant-[A-Za-z0-9_-]{20,}|sk-(proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{30,}|ya29\.[0-9A-Za-z_-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.'
if [ "$MODO" = commit ]; then
  linea=$(/usr/bin/grep -aEn "$PATRONES" "$METADATA" 2>/dev/null \
    | /usr/bin/sed -n '1{s/:.*//;p;}')
  if [ -n "$linea" ]; then
    rojo "BLOQUEADO · posible credencial en metadata del commit (linea $linea)"
    FALLO=1
  fi
fi
for f in "${ARCHIVOS[@]}"; do
  if ! comprobar_lectura "$f"; then
    rojo "BLOQUEADO · no pude leer: $(ruta_visible "$f")"; FALLO=1; continue
  fi
  linea=$(leer_archivo "$f" | grep -aEn "$PATRONES" 2>/dev/null | sed -n '1{s/:.*//;p;}')
  if [ -n "$linea" ]; then
    rojo "BLOQUEADO · posible credencial en: $(ruta_visible "$f") (linea $linea)"
    FALLO=1
  fi
done

# 3) asignaciones sospechosas con valor literal
ASIGNACION='(password|passwd|contrasena|contraseña|secret|client[_-]?secret|api[_-]?key|token)[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9+/_=-]{16,}'
if [ "$MODO" = commit ]; then
  linea=$(/usr/bin/grep -aiEn "$ASIGNACION" "$METADATA" 2>/dev/null \
    | /usr/bin/sed -n '1{s/:.*//;p;}')
  if [ -n "$linea" ]; then
    rojo "REVISAR · asignacion sospechosa en metadata del commit (linea $linea)"
    FALLO=1
  fi
fi
for f in "${ARCHIVOS[@]}"; do
  if ! comprobar_lectura "$f"; then
    [ "$FALLO" -eq 1 ] || rojo "BLOQUEADO · no pude leer: $(ruta_visible "$f")"
    FALLO=1; continue
  fi
  linea=$(leer_archivo "$f" | grep -aiEn "$ASIGNACION" 2>/dev/null | sed -n '1{s/:.*//;p;}')
  if [ -n "$linea" ]; then
    rojo "REVISAR · asignacion sospechosa en: $(ruta_visible "$f") (linea $linea)"
    FALLO=1
  fi
done

[ $FALLO -eq 0 ] && { verde "limpio · ninguna credencial detectada"; exit 0; }
rojo "ABORTAR: corrige lo anterior antes de subir."; exit 1
