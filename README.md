# Orquesta IA

Orquestador local de varias cuentas de IA (Claude, GPT/Codex, Gemini) desde una sola
terminal o un panel web. Reparte cada tarea a la cuenta que mejor rinde en ese tipo de
trabajo y que todavía tiene cupo, lleva la contabilidad de tokens, y permite que los
modelos se auditen entre ellos.

## Qué hace

- **Multi-cuenta real.** Cada cuenta vive en su propio directorio aislado
  (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`), así que varias cuentas del mismo proveedor
  conviven sin pisarse.
- **Routing por tarea.** `code`, `agentic`, `reasoning`, `review`, `writing`,
  `research`, `edicion`, `bulk`. El puntaje combina el peso técnico que le asignes,
  el rendimiento que hayas medido, y la holgura de cupo que le quede a la cuenta.
- **Ventanas de recarga.** Registra el gasto dentro de la ventana de cada plan
  (5 h en los planes de suscripción) y baja el puntaje de las cuentas que se están
  quedando sin cupo, antes de que choquen el límite.
- **Detección de límite.** Si un proveedor responde con un límite de uso, la cuenta
  queda excluida del routing hasta la hora de recarga, y vuelve sola.
- **Auditoría cruzada.** Todas responden la misma pregunta y luego cada una audita
  las respuestas de las otras señalando errores, omisiones y una nota.
- **Contabilidad.** Cada llamada queda en `state/ledger.jsonl` con cuenta, tokens,
  segundos y tarea.

## Instalación

```sh
git clone <este-repo> ~/Documentos/ia/orquesta
cd ~/Documentos/ia/orquesta
cp profiles.example.json profiles.json      # edítalo con tus cuentas
ln -sf "$PWD/orq" ~/.local/bin/orq
echo '[ -f "$HOME/Documentos/ia/orquesta/shell.sh" ] && . "$HOME/Documentos/ia/orquesta/shell.sh"' >> ~/.bashrc
```

Requiere Python 3.9+ y los CLIs que vayas a usar (`claude`, `codex`, `gemini`).

## Uso

```sh
orq cuentas                  # estado de cada cuenta, cupo y recarga
orq status                   # lo anterior + gasto + rendimiento medido
orq route code               # a quién delegaría (no gasta tokens)

orq ask "arregla este bug" --tarea code
orq ask "..." --perfil claude-personal      # forzar una cuenta
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
red. Tiene pestañas de Panel, Consultar, Ajustes e Historial: editar pesos por tarea,
cupos, topes diarios, lanzar consultas y calificar resultados.

Para dejarlo permanente hay un servicio de usuario en
`~/.config/systemd/user/orquesta.service`.

## Estructura

```
orq                  CLI
orqlib.py            núcleo compartido (estado con bloqueo fcntl)
orqweb.py            panel web (API + jobs asíncronos)
web/index.html       interfaz
shell.sh             integración de terminal
tools/scan-secretos.sh   escáner de credenciales (corre en pre-commit)
profiles.json        tu configuración real        ← NO versionado
accounts/<id>/       credenciales por cuenta      ← NO versionado
state/               ledger, límites, puntajes    ← NO versionado
```

## Sobre usar varias cuentas

Tener cuentas separadas por propósito (personal, empresa, cliente) y elegir la que
corresponde a cada trabajo es su uso previsto. Agrupar varias suscripciones para
sortear los límites de uso va contra los términos de servicio de los proveedores;
esta herramienta no rota cuentas automáticamente al chocar un límite, precisamente
por eso: cuando una cuenta se bloquea, lo dice y espera la recarga.

Ver [SECURITY.md](SECURITY.md).

## Licencia

MIT
