# Orquesta IA

Orquestador local de varias cuentas de IA (Claude, GPT/Codex y Antigravity) desde una
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
  `RESOURCE_EXHAUSTED`, bloquea la cuenta hasta la recarga y Kitti prueba el siguiente
  motor de texto cuando el fallo fue temprano y seguro de reintentar.
- **Auditoría cruzada.** Todas responden la misma pregunta y luego cada una audita
  las respuestas de las otras señalando errores, omisiones y una nota.
- **Contabilidad.** Cada llamada queda en `state/ledger.jsonl` con cuenta, tokens,
  segundos, tarea, sesión, carpeta y una vista previa de 200 caracteres del prompt.

## Instalación

```sh
git clone <este-repo> ~/Documentos/ia/orquesta
cd ~/Documentos/ia/orquesta
cp profiles.example.json profiles.json      # edítalo con tus cuentas
ln -sf "$PWD/orq" ~/.local/bin/orq
echo '[ -f "$HOME/Documentos/ia/orquesta/shell.sh" ] && . "$HOME/Documentos/ia/orquesta/shell.sh"' >> ~/.bashrc
```

Requiere Python 3.9+ y los CLIs que vayas a usar (`claude`, `codex`, `agy`).

## Uso

```sh
orq cuentas                  # estado de cada cuenta, cupo y recarga
orq status                   # lo anterior + gasto + rendimiento medido
orq route code               # a quién delegaría (no gasta tokens)

orq ask "arregla este bug" --tarea code
orq ask "..." --perfil claude-personal      # forzar una cuenta
orq imagen "ilustración del producto"        # Antigravity / Nano Banana
orq fan "..."                               # preguntar a todas a la vez
orq audit "..."                             # responden y se auditan entre ellas

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
contraseñas. Cada proveedor guarda su propio token en `accounts/<id>/`, que está
excluido de git.

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
orqchat.py           conversación de lenguaje natural en Kitti
orqweb.py            panel web (API + jobs asíncronos)
orquesta-app.py      envoltorio GTK4/WebKit del panel
web/index.html       interfaz
shell.sh             integración de terminal
tests/               regresiones de routing, Kitti, cuotas, uso y seguridad web
tools/scan-secretos.sh   escáner de credenciales (corre en pre-commit)
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
python3 -m unittest discover -v
bash -n shell.sh tools/scan-secretos.sh
tools/scan-secretos.sh
```

Ver [SECURITY.md](SECURITY.md).

## Licencia

MIT
