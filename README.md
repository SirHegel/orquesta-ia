# Orquesta IA

Orquestador local de varias cuentas de IA (Claude, GPT/Codex, Antigravity y MiniMax) desde una
sola terminal o un panel web. Primero filtra por capacidad del motor, luego usa la
máxima potencia configurada y reparte entre cuentas equivalentes según su cupo y uso.
También lleva la contabilidad de tokens y permite auditorías cruzadas.

## Qué hace

- **Multi-cuenta real.** Cada cuenta vive en su propio directorio aislado
  (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`), así que varias cuentas del mismo proveedor
  conviven sin pisarse.
- **Capacidades antes que puntajes.** Claude y Codex compiten en las tareas de texto;
  Antigravity/Nano Banana queda reservado para `imagen`. Los pesos de especialidad
  solo ayudan a repartir un proyecto y nunca convierten un motor visual en chat.
- **Potencia parametrizada.** `power` define la potencia efectiva por perfil o tarea.
  A igual potencia, decide el rendimiento medido, la cuota disponible y el reparto de
  consumo entre cuentas del mismo proveedor.
- **Ventanas de recarga.** Registra el gasto dentro de la ventana de cada plan
  (5 h en los planes de suscripción) y baja el puntaje de las cuentas que se están
  quedando sin cupo, antes de que choquen el límite.
- **Detección de límite y continuidad.** Conserva errores estructurados como 429 y
  `RESOURCE_EXHAUSTED`. Ante timeout, cuota o fallo, entrega al siguiente motor el
  encargo original, sesiones previas, archivos cambiados y estado real de Git para
  que audite y continúe en vez de empezar de cero.
- **Escritura coordinada.** Un lock por repositorio impide que dos terminales de
  Orquesta escriban el mismo árbol a la vez. Dentro de un proyecto las tareas que
  escriben son exclusivas; las investigaciones de solo lectura sí pueden ir en paralelo.
- **Cierre verificable.** La IA propone tests, pero Orquesta ejecuta por sí misma solo
  comandos permitidos, ignora el `rc` declarado por el modelo y repite
  verificación/reparación antes de decir `LISTO`. Con publicación activa, las pruebas
  corren en checkouts detached frescos del mismo commit inmutable que se publicará;
  sin publicación sigue siendo una verificación controlada del workspace vivo.
- **Publicación segura.** `orq publicar` escanea árbol, índice y commits locales,
  bloquea credenciales, hace commit y push sin force. Los procesos IA heredan además
  un pre-push preventivo para no saltarse accidentalmente ese gate.
- **Auditoría cruzada.** Todas responden la misma pregunta y luego cada una audita
  las respuestas de las otras señalando errores, omisiones y una nota.
- **Contabilidad.** Cada llamada queda en `state/ledger.jsonl` con cuenta, tokens,
  segundos, tarea, sesión, carpeta y una vista previa de 200 caracteres del prompt.

## Instalación portable

```sh
git clone <este-repo> "$HOME/.local/share/orquesta"
cd "$HOME/.local/share/orquesta"
install -m 600 profiles.example.json profiles.json  # edítalo con tus cuentas
mkdir -p "$HOME/.local/bin"
ln -sf "$PWD/orq" ~/.local/bin/orq
ln -sf "$PWD/tools/minimax" ~/.local/bin/minimax
```

`ORQ_HOME` es la raíz canónica de **una instalación completa**, no una carpeta
de datos independiente. Al cargar `shell.sh`, su ubicación real es la elección
más reciente del operador: este valida la copia y reemplaza cualquier
`ORQ_HOME` heredado. `orq` y `minimax` usan esa misma raíz aunque un enlace
encontrado en `PATH` apunte accidentalmente a otro clon: el shell define ambos
comandos contra su propia raíz. Fuera de ese shell, la ubicación real del
entrypoint selecciona la copia; si `ORQ_HOME` ya existe debe coincidir
literalmente con esa raíz canónica y actúa solo como una aserción de
consistencia, nunca como una ruta que Python abra o ejecute.

La instalación debe contener `orq`, `orqlib.py`, `orqroot.py`, `shell.sh` y
`tools/minimax` como archivos regulares exactamente en esas ubicaciones:
ninguno ni sus directorios intermedios puede ser un symlink. La propia raíz o
el enlace externo usado para invocar un entrypoint sí pueden ser symlinks y se
canonicalizan. Si la copia no es válida, o un binario B se invoca explícitamente
desde el shell de A, el comando falla en vez de mezclar código y estado. Para
cambiar de copia, carga el `shell.sh` de esa copia. La configuración
`shell.local.sh` ni un `ORQ_HOME` exportado después pueden reemplazar esa raíz
después de validarla. Al cargar otra copia se limpian además las cuentas, el
override manual, la caché y la política procedentes de la instalación anterior.

La implementación soportada requiere Linux con procfs montado (`/proc` y
`/proc/self/fd`), Bash y las utilidades GNU instaladas en las rutas de sistema que usa
el código (`/usr/bin`, `/bin`), además de Python 3.9+, Git y los CLIs que vayas a usar
(`claude`, `codex`, `agy`). El supervisor, el scanner y las operaciones por descriptor
fallan cerrados si falta ese toolchain; no se resuelven sustitutos desde un `PATH`
heredado.
La ruta del clon y el emulador de terminal no importan: Kitty, GNOME Terminal,
Konsole, WezTerm, Alacritty o una consola SSH pueden ejecutar el mismo comando:

```sh
orq chat
```

Esto no arranca una IA al abrir la terminal. `orq` también funciona desde Zsh,
Fish y otros shells porque es un ejecutable independiente. `shell.sh` solo agrega
atajos y sincronización a Bash; es opcional. Para cargarlo, guarda la ruta real del
clon en `~/.bashrc`:

```sh
printf '\n[ -f %q ] && . %q\n' "$PWD/shell.sh" "$PWD/shell.sh" >> "$HOME/.bashrc"
```

### Autoarranque y permisos: decisiones locales

El autoarranque es **opt-in** y se configura fuera de Git. Crea el archivo bajo
`XDG_CONFIG_HOME` (o `~/.config` si no está definido) solo en la máquina donde lo
quieras:

```sh
orq_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/orquesta"
mkdir -p "$orq_config_dir"
cat > "$orq_config_dir/shell.local.sh" <<'EOF'
# Una lista limita el autoarranque a esos emuladores; 1 significa cualquiera.
ORQ_AUTO_CHAT=kitty             # ejemplos: kitty,wezterm  o  1

# Opcional y sensible: hace que los CLIs de proveedores omitan sus confirmaciones.
ORQ_PERMISOS_TOTALES=1
EOF
chmod 600 "$orq_config_dir/shell.local.sh"
unset orq_config_dir
```

La ubicación real es
`${XDG_CONFIG_HOME:-$HOME/.config}/orquesta/shell.local.sh`. Ese archivo es un
**trust anchor** explícito: al aceptarlo, Bash ejecutará su contenido con los permisos
del usuario. Por eso Orquesta solo lo carga si toda la ruta está libre de symlinks y el
archivo es regular, pertenece al UID actual, tiene un único enlace, no permite escritura
de grupo ni de otros y ocupa como máximo 64 KiB. Symlinks, hardlinks, FIFO y archivos
demasiado grandes se rechazan sin evaluar su contenido.

Sin ese archivo, `ORQ_AUTO_CHAT=0` y `ORQ_PERMISOS_TOTALES=0`: un clon no abre
ninguna IA ni agrega flags de omisión a `claude`, `codex`, `agy` o `gemini`.
Los wrappers que una carga anterior hubiera creado vuelven a comprobar la política
en cada invocación, por lo que desactivarla surte efecto sin abrir otra terminal.
Para omitir una vez un autoarranque configurado: `ORQ_SIN_MODO_PROMPT=1 bash`.

El terminal solo presenta el proceso. El acceso al disco, navegador u otras
herramientas depende del sandbox/flags del CLI proveedor y de los permisos Unix del
usuario; instalar Orquesta o usar Kitty no concede esos permisos por sí solo. Las
credenciales siguen siendo locales en `accounts/`, `profiles.json` y `state/`, todos
excluidos de Git.

Para control real de pestañas con Claude, instala y autoriza una sola vez la
extensión oficial **Claude in Chrome**, y configura el perfil privado con
`"chrome": true` (o crea uno con `orq cuenta add ... --provider claude --chrome`).
También puedes usar `"chrome": "auto"`: no añade el flag hasta detectar la
extensión oficial instalada, y la habilita automáticamente desde entonces.
Orquesta añadirá `--chrome` solo a ese perfil. La extensión conserva sus propios
controles para sitios y acciones sensibles; no se versiona ninguna sesión del
navegador. Guía oficial: <https://support.claude.com/en/articles/12012173-get-started-with-claude-in-chrome>.

## Uso

```sh
orq chat                     # interfaz natural en cualquier terminal
orq cuentas                  # estado de cada cuenta, cupo y recarga
orq status                   # lo anterior + gasto + rendimiento medido
orq route code               # a quién delegaría (no gasta tokens)

orq ask "arregla este bug" --tarea code
orq ask "..." --perfil claude-personal      # forzar una cuenta
orq imagen "ilustración del producto"        # Antigravity / Nano Banana
orq fan "..."                               # preguntar a todas a la vez
orq audit "..."                             # responden y se auditan entre ellas
orq publicar --en /ruta/al/repo              # scan -> commit -> push seguro

orq score <run_id> 9 --tarea code           # calificar → entrena el router
orq cupo claude-personal 900000             # tope por ventana de recarga
orq budget gpt-personal 500000              # tope por día
orq limites                                 # cuentas bloqueadas por límite
orq web                                     # panel en http://127.0.0.1:8787
```

Para fijar una cuenta en la terminal actual (útil para usar `claude` o `codex`
directamente, sin pasar por `orq`):

```sh
orquse claude-trabajo    # exporta CLAUDE_CONFIG_DIR para esta terminal
orqoff                   # vuelve a las cuentas por defecto
```

## Agregar una cuenta

```sh
orq cuenta add claude-trabajo --provider claude --plan max --proposito trabajo
# el comando de login que imprime hay que correrlo a mano: es OAuth interactivo
```

El login **nunca** es automático y el proyecto **nunca** pide, guarda ni transmite
contraseñas. Un home de cuenta solo puede ser una de estas rutas exactas, sin symlinks
en sus componentes:

- un home administrado: `BASE/accounts/<id>` (`BASE` es la raíz canónica de la
  instalación) o
  `~/.local/share/orquesta/accounts/<id>`;
- el home oficial del proveedor correspondiente: `~/.claude` para Claude,
  `~/.codex` para GPT/Codex, `~/.gemini` para Gemini o
  `~/.gemini/antigravity-cli` para Antigravity.

MiniMax solo admite uno de los homes administrados. No basta con que una ruta arbitraria
esté dentro de la instalación o del directorio personal. Cuando una cuenta usa API key,
la única ubicación admitida es `<home>/api_key`; se crea y se exige con modo privado
`0600`, y nunca se reutiliza otro archivo de credenciales del proveedor.

`orq cuenta rm <id> --purge` vacía de forma segura el home administrado directo
`BASE/accounts/<id>`. Para evitar carreras entre validar un inode y borrar otro nombre,
trunca por descriptor los archivos regulares y conserva sus entradas y directorios ya
vacíos. Symlinks, FIFO y otros tipos especiales se preservan y hacen fallar cerrado la
purga. El botón de login del panel abre el CLI mediante `orqlogin.py` y
argumentos fijos, sin construir un script `bash -lc`.

En el chat, `/shell` es deliberadamente una consola de diagnóstico de solo lectura.
Admite únicamente `pwd`, `ls`, `git status`, `git diff --stat` y `git log`. El texto
libre nunca se interpreta como comando; para cualquier otra operación usa una terminal
separada.

### Proyectos y GitHub

`orq proyecto` siempre verifica antes de cerrar. Usa `--publicar` para que, solo si
todo queda limpio, ejecute el gate de secretos, cree el commit y haga push. En una
máquina propia se puede dejar como política local añadiendo a `profiles.json` (archivo
privado y excluido de Git):

```json
"_publicar_github": true
```

Un clon nuevo conserva esta política desactivada: no debe escribir en un remoto sin
que su dueño lo decida. El gate falla cerrado si no puede leer un blob, si `origin`
contiene una credencial, si la rama remota va por delante o si cualquier escaneo
detecta una ruta/valor sensible. Nunca fuerza una rama sin un lease exacto del OID
remoto previamente inventariado.

Para `orq proyecto --publicar`, Orquesta construye un tree con bytes crudos
(`--no-filters`), crea con `commit-tree` el OID candidato sin mover refs ni el índice y
lo escanea. La revisión IA y cada comando objetivo reciben un checkout detached fresco
de ese OID. Al publicar vuelve a construir el tree vivo, exige igualdad exacta, fija la
rama con compare-and-swap y empuja `OID:refs/heads/rama`; no reconstruye el commit desde
un worktree que pudo cambiar después de las pruebas. Archivos ignorados no forman parte
del snapshot; symlinks, submódulos y flags `skip-worktree`/`assume-unchanged` se rechazan
porque pueden depender de bytes no representados. El commit candidato usa la identidad
técnica `Orquesta IA <orquesta@localhost>` y no lleva firma criptográfica: un ruleset
que exija commits firmados rechazará el push y conservará el commit solo localmente.
Las refs del destino se obtienen desde el `pushurl` literal en un namespace efímero,
se contrastan con `ls-remote` y se retiran antes del push; refs `origin/*` o refspecs
locales no sirven como evidencia. El push usa lease sobre el OID remoto observado y
configura upstream tras crear una rama. Repositorios sin primer commit generan un
commit raíz verificable. Replace refs, grafts y límites shallow no pueden alterar el
recorrido auditado y los dos últimos se rechazan.
Como un linked worktree delega su metadata mediante un archivo `.git` fuera de la
capacidad adquirida, hoy se rechaza explícitamente: ejecute la publicación verificable
desde el checkout principal. El fetch de inventario no modifica `FETCH_HEAD` y, tras
un push exitoso, el ref local `origin/rama` se actualiza mediante compare-and-swap.

### Cuentas por API key (MiniMax)

MiniMax habla el protocolo de Anthropic, así que reusamos el binario `claude`
apuntándolo a su endpoint. No hay OAuth: se paga con una API key. Para impedir que la
key se envíe a un host configurado por datos no confiables, la allowlist acepta
exclusivamente los endpoints oficiales `https://api.minimax.io/anthropic` y
`https://api.minimaxi.com/anthropic`.

```sh
orq cuenta add minimax --provider minimax --plan api --ventana 1
orq cuenta key minimax             # pide la key por stdin, la guarda en modo 600
minimax                            # sesión interactiva contra MiniMax-M3[1m]
minimax -p "resume este repo"
```

Las variables `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` se inyectan **solo en
el proceso hijo**; nunca se exportan al `entorno.sh` global, porque pisarían la
cuenta Claude real de todas las terminales. Para fijarlo en una sola terminal:
`orquse minimax` (y `orqoff` para volver).

## Panel web

`orq web` levanta el panel en `127.0.0.1:8787` — solo loopback, nunca expuesto a la
red. Tiene vistas de Cuentas, Consultar y Uso para administrar perfiles, lanzar
consultas, ver consumo y calificar resultados.

Para dejarlo permanente hay un servicio de usuario en
`~/.config/systemd/user/orquesta.service`.

## Estructura

```
orq                  CLI
orqlib.py            núcleo compartido (estado con bloqueo fcntl)
orqroot.py           contrato y validación compartidos de ORQ_HOME
orqlogin.py          login gráfico con argv fijo y validación fail-closed
orqrun.py            supervisor aislado de procesos con modos y argv validados
orqchat.py           conversación natural (Kitty mejora la presentación gráfica)
orqweb.py            panel web (API + jobs asíncronos)
orquesta-app.py      envoltorio GTK4/WebKit del panel
web/index.html       interfaz
shell.sh             integración Bash opcional; autoarranque local opt-in
tests/               regresiones de routing, Kitti, cuotas, uso y seguridad web
.github/workflows/tests.yml  CI de compilación, sintaxis y suite completa
tools/scan-secretos.sh   escáner de árbol, índice y commits
tools/git-hooks/pre-push gate heredado por procesos IA
profiles.json        tu configuración real        ← NO versionado
accounts/<id>/       credenciales por cuenta      ← NO versionado
state/               ledger, límites, puntajes    ← NO versionado
```

## Sobre usar varias cuentas

Tener cuentas separadas por propósito (personal, empresa, cliente) y elegir la que
corresponde a cada trabajo es su uso previsto. Kitti puede continuar con otra cuenta
compatible cuando una falla antes de producir trabajo; no repite automáticamente un
timeout que pudo haber modificado archivos.

## Pruebas

```sh
python3 -m unittest discover -s tests -v
bash -n shell.sh tools/scan-secretos.sh
tools/scan-secretos.sh
```

El workflow `tests` compila entrypoints, valida Bash, ejecuta
`tools/scan-secretos.sh --todo` y corre la suite completa en cada `push` a `main` y PR con
permisos `contents: read`; las acciones externas están fijadas por SHA.

Ver [SECURITY.md](SECURITY.md).

## Licencia

MIT
