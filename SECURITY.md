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

`tools/scan-secretos.sh` corre automáticamente como hook `pre-commit` y **aborta el
commit** si detecta:

1. Rutas sensibles entre los archivos preparados.
2. Patrones de credencial en el contenido: `sk-ant-…`, `sk-…`, `ghp_/gho_/ghs_/ghu_/ghr_…`,
   `AIza…` (Google), `ya29.…` (OAuth Google), claves privadas PEM, JWT.
3. Asignaciones literales de `password`, `secret`, `api_key`, `token`.

Correrlo a mano sobre todo el repositorio:

```sh
tools/scan-secretos.sh
```

Si el hook se pierde (los hooks no viajan en un clone), reinstálalo:

```sh
printf '#!/usr/bin/env bash\nexec "$(git rev-parse --show-toplevel)/tools/scan-secretos.sh" --staged\n' \
  > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
```

## Superficie de red

El panel escucha **solo en `127.0.0.1`**. No hay bind a `0.0.0.0`, no hay autenticación
remota porque no hay acceso remoto. Si necesitas llegar desde otra máquina, usa un túnel
SSH (`ssh -L 8787:127.0.0.1:8787 …`) en vez de exponer el puerto.

Las respuestas de la API van con `X-Content-Type-Options: nosniff` y `Cache-Control: no-store`.
La interfaz escapa todo el contenido que viene del servidor antes de insertarlo en el DOM,
para que una respuesta de un modelo no pueda inyectar HTML en el panel.

## Permisos de disco

Los directorios de cuenta se crean con modo `700`. Verifícalo:

```sh
find accounts -maxdepth 1 -type d -exec stat -c '%a %n' {} \;
```

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
