# Seguridad

## Qué nunca entra en el repositorio

`.gitignore` excluye, y `tools/scan-secretos.sh` verifica en cada commit:

| Ruta | Contiene |
|---|---|
| `accounts/` | tokens OAuth y API keys de cada cuenta |
| `profiles.json` | tu configuración real, con rutas locales |
| `state/` | ledger de uso, límites, puntajes |
| `.env`, `*.pem`, `*.key`, `*api_key*`, `*credential*` | secretos en general |

## Escáner de credenciales

`tools/scan-secretos.sh` se ejecuta en CI sobre todo el repositorio. También puede
instalarse manualmente como hook local `pre-commit` opt-in; no se instala
automáticamente y los hooks no viajan en un clone. El escáner **aborta** si detecta:

1. Rutas sensibles entre los archivos preparados.
2. Patrones de credencial en el contenido: `sk-ant-…`, `sk-…`, `ghp_/gho_/ghs_/ghu_/ghr_…`,
   `AIza…` (Google), `ya29.…` (OAuth Google), claves privadas PEM, JWT.
3. Asignaciones literales de `password`, `secret`, `api_key`, `token`.

Correrlo a mano sobre todo el repositorio:

```sh
tools/scan-secretos.sh
```

Para habilitar el `pre-commit` local de forma explícita:

```sh
printf '#!/usr/bin/env bash\nexec "$(git rev-parse --show-toplevel)/tools/scan-secretos.sh" --staged\n' \
  > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
```

Este hook opt-in es distinto del `pre-push` defensivo en
`tools/git-hooks/pre-push`: Orquesta configura este último únicamente en el entorno
de los procesos de proveedor que lanza, sin modificar los hooks persistentes del
repositorio.

## Superficie de red

El panel escucha **solo en `127.0.0.1`**. Además, la API valida `Host`, rechaza un
`Origin` ajeno y exige `application/json` en todos los POST. Estas comprobaciones
evitan solicitudes desde páginas externas y ataques de DNS rebinding contra el servicio
local. Si necesitas llegar desde otra máquina, usa un túnel SSH
(`ssh -L 8787:127.0.0.1:8787 …`) en vez de exponer el puerto.

Las API keys de MiniMax solo pueden enviarse a la allowlist cerrada de endpoints
oficiales `https://api.minimax.io/anthropic` y
`https://api.minimaxi.com/anthropic`; cualquier otro `base_url` falla antes de lanzar
el proceso proveedor.

Las respuestas de la API van con CSP, `X-Frame-Options: DENY`,
`X-Content-Type-Options: nosniff` y `Cache-Control: no-store`.
La interfaz escapa todo el contenido que viene del servidor antes de insertarlo en el DOM,
para que una respuesta de un modelo no pueda inyectar HTML en el panel.

## Frontera de procesos y plataforma

Orquesta está soportada en Linux con procfs, Bash, Python y Git disponibles en sus
rutas de sistema. El supervisor usa subreaper/grupos de procesos y, cuando existe un
bus de usuario operativo, un scope de systemd para terminar descendientes al agotar el
tiempo o la salida. Esa frontera controla ciclo de vida, número de jobs, workers,
tiempo y bytes retenidos; no es un sandbox contra código deliberadamente hostil del
mismo UID ni promete un límite de memoria del sistema cuando systemd no está
disponible. Para código no cooperativo use además un contenedor/cgroup administrado por
el operador. No se heredan `PATH`, loaders ni configuración Git para sustituir el
toolchain fijo.

## Modelo de amenaza de CodeQL

Orquesta es una herramienta local, pero no presupone que todo dato local sea seguro.
Los argumentos del CLI, el entorno, `profiles.json`, archivos de sesión y entradas del
chat se analizan con el modelo de amenaza `local` de CodeQL. Una ruta solo se usa tras
canonicalizarla y comprobar una capacidad concreta. Los homes no son cualquier
descendiente de la instalación o del directorio personal: solo se admiten exactamente
`BASE/accounts/<id>`, `~/.local/share/orquesta/accounts/<id>` o el home oficial que
corresponda al proveedor (`~/.claude`, `~/.codex`, `~/.gemini` o
`~/.gemini/antigravity-cli`). MiniMax no tiene una excepción de home oficial. Se
rechazan enlaces simbólicos en cualquier componente sensible. Los workspaces deben
estar bajo las raíces autorizadas o bajo un directorio temporal privado (propietario
actual y modo `0700` en su raíz).

`/shell` ya no interpreta texto con un shell. Solo acepta los diagnósticos exactos
`pwd`, `ls`, `git status`, `git diff --stat` y `git log`, despachados como `argv`
constante, sin hooks, paginador ni diff externo. Para ejecutar otro comando, usa una
terminal separada: convertir conversación o estado recuperado en código de shell no es
una capacidad segura.

`ORQ_HOME` solo selecciona una instalación completa y reconocible. La raíz se
canonicaliza (por eso ella sí puede llegar mediante un symlink), pero cada
marcador debe ser un archivo regular en su ubicación exacta: no se admiten
symlinks finales ni en directorios intermedios, aunque apunten dentro de la
instalación. El entrypoint de esa misma raíz toma la ejecución antes de cargar el
estado. El código Python deriva la raíz exclusivamente de su propio `__file__`;
jamás usa `ORQ_HOME` para abrir ni ejecutar una ruta. Esa variable solo debe
coincidir literalmente con la raíz canónica como aserción de consistencia. El
shell enlaza sus funciones `orq` y `minimax` directamente con la copia elegida;
invocar explícitamente un binario de otra copia falla de forma cerrada.
El shell conserva aparte la raíz elegida y la reafirma antes de cada operación,
por lo que cambiar accidentalmente `ORQ_HOME` después no selecciona otra copia;
para hacerlo se debe cargar el `shell.sh` de esa instalación. Ese cambio restaura
primero el entorno original y descarta la caché, los indicadores de cuenta, el
override manual y la política de la raíz anterior antes de cargar el nuevo estado.
La configuración local más reciente se aplica después de esa limpieza. Además,
los wrappers que pueden omitir el sandbox vuelven a validar la política en cada
invocación: pasar de `ORQ_PERMISOS_TOTALES=1` a `0` no deja flags peligrosos vivos.

`${XDG_CONFIG_HOME:-$HOME/.config}/orquesta/shell.local.sh` es una excepción
deliberada: constituye un trust anchor local y, si se acepta, Bash evalúa su contenido
con los permisos del usuario. Antes de hacerlo, Orquesta exige una ruta sin symlinks y
un archivo regular del UID actual, con `st_nlink == 1`, sin escritura para grupo u
otros y de no más de 64 KiB. Symlinks, hardlinks, FIFO y archivos sobredimensionados
fallan cerrados sin evaluar siquiera contenido parcial.

La selección es local: no se lee de una petición web ni de una ruta persistida
en `profiles.json`. Al cargar `shell.sh`, la ubicación validada de ese archivo
reemplaza una variable heredada porque es la elección más reciente del operador,
y se reafirma después de cargar `shell.local.sh` para que las demás preferencias
no separen código y estado.

La frontera no confiable principal es la entrada HTTP del panel, aun cuando este
escuche solo en loopback. CodeQL se mantiene deliberadamente en el modelo más estricto
`local` (además del modelo remoto predeterminado): además de revisar todos los datos
HTTP, ayuda a detectar cuándo un
identificador local llega accidentalmente a una ruta o a una invocación de proceso. Los
identificadores que sí se convierten en nombres de archivo (sesión, `run_id`, notas de
memoria y nombre de imagen) se acotan, vuelven opacos o canonicalizan para impedir
traversal, enlaces fuera de su raíz e inyección accidental de opciones.

El login gráfico tampoco construye `bash -lc`: lanza un helper Python fijo y entrega el
identificador del perfil únicamente por entorno. El helper vuelve a validar id,
proveedor, navegador y home antes de ejecutar uno de los entrypoints constantes.

El estado JSON se escribe con un temporal aleatorio `0600` y reemplazo atómico; las
lecturas rechazan symlinks y archivos no regulares. Los recorridos de sesiones no
siguen enlaces, deduplican directorios por dispositivo/inodo y tienen límites de
profundidad y cantidad. Estas fronteras se cubren con pruebas adversariales y con CI en
cada PR.

La ausencia de `profiles.json` representa una instalación todavía sin configurar. Si
el archivo existe debe ser un regular privado del usuario (modo `0600`, sin symlinks ni
hardlinks), de tamaño acotado y con JSON válido; cualquier incumplimiento detiene CLI,
helpers y panel con un error visible. Nunca se sustituye una configuración existente
por el valor vacío como consecuencia de un error de lectura.

La verificación final sin publicación es **controlada**, no estrictamente de solo
lectura: tests, linters y compiladores pueden escribir cachés dentro del workspace. Con
publicación activa se usa otra frontera. Orquesta hashea bytes regulares publicables sin
filtros Git, crea un `commit-tree` sin refs y ejecuta la revisión y cada comprobación en
un worktree detached fresco materializado directamente desde sus blobs. Archivos
ignorados no se copian; symlinks, gitlinks, `skip-worktree` y `assume-unchanged` fallan
cerrados. Antes de publicar reconstruye el tree vivo, fija el índice con ese tree,
actualiza la rama mediante compare-and-swap y empuja el OID exacto probado. Un cambio
posterior del worktree o índice no puede entrar en ese commit. El commit técnico usa
`Orquesta IA <orquesta@localhost>` y deliberadamente no se firma; repositorios cuyo
ruleset exija firma rechazarán el push.
El inventario remoto procede del `pushurl` literal mediante un namespace efímero y se
contrasta con `ls-remote`; nunca se confía en `refs/remotes/origin` ni en el refspec
configurable. Ese namespace se elimina por CAS antes de empujar. El ref remoto se
actualiza con lease exacto, y una rama unborn usa un commit raíz y CAS contra el OID
cero. `GIT_NO_REPLACE_OBJECTS`, archivos alternos vacíos para shallow/grafts y una
auditoría descriptor-relativa impiden que esas topologías oculten ancestros.
Los linked worktrees de entrada se rechazan explícitamente hasta poder atestiguar por
descriptor tanto su gitdir privado como el gitdir común; la operación debe iniciarse
desde el checkout principal. El fetch efímero usa `--no-write-fetch-head`, y el ref
local de seguimiento se actualiza por CAS después de confirmar el push exacto.

## Permisos de disco

Los directorios de cuenta se crean con modo `700`. Una API key solo puede ocupar el
archivo dedicado `<home>/api_key`, creado y leído como secreto privado `0600`; un
`api_key_file` que apunte a cualquier otra ubicación se rechaza. Verifícalo:

```sh
find accounts -maxdepth 1 -type d -exec stat -c '%a %n' {} \;
```

La opción `orq cuenta rm <id> --purge` solo opera sobre el home administrado exacto
`BASE/accounts/<id>` mediante descriptores ya validados. Trunca los archivos regulares
por su descriptor y conserva entradas y directorios vacíos: un `unlink` por nombre no
puede ligarse al inode previamente validado. Symlinks y tipos especiales se preservan y
hacen fallar la purga, sin tocar sus destinos.

## Manejo de credenciales

Este proyecto **no pide, no guarda y no transmite contraseñas**. Cada login es
interactivo y lo hace el CLI del proveedor (`claude` con `/login`, `codex login`);
el token queda bajo `accounts/<id>/`, gestionado por ese CLI. `orq cuenta login` solo
imprime el comando que debes correr tú.

## Si se filtró un secreto

1. Revócalo en el proveedor **primero** (rotar la credencial es lo único que corta el
   acceso; borrarlo del historial no).
2. Reescribe el historial (`git filter-repo` o `git rebase -i`) y fuerza el push.
3. Asume que cualquier commit que llegó a un remoto ya fue leído.

## Reportar

Si encuentras un fallo de seguridad, abre un issue **sin** incluir el secreto ni
datos reales.
