# devws

Front local para desarrollar en varios repos a la vez: ramas, worktrees,
servicios y "espacios" (perfiles). Servidor FastAPI en un solo fichero
(`server.py`) + UI en JS sin framework (`static/index.html`) + servidor MCP
(montado en el mismo FastAPI) para que Copilot cree y use espacios.

## Donde se usa

- El uso real es en un Mac de empresa, con los repos reales y su `config.toml`.
  Aqui (Windows o la nube) no hay ninguno de esos repos: se desarrolla y se
  prueba con repos falsos, se sube a GitHub y el usuario hace `git pull` en el Mac.
- `config.toml`, `profiles/`, `state.json`, `logs/` y `worktrees/` NO se
  versionan. La plantilla es `config.example.toml`.
- **El repo es PUBLICO.** Nunca commitear nombres de empresa, paths reales,
  nombres de registry de paquetes, tokens ni URLs de remotos. Usar solo los
  nombres genericos: api-gateway, search-service, web-portal, core-lib,
  db-service. `config.toml.bak` (si existe) tiene la config real: nunca commitearlo.

## Convenciones

- Simplicidad: todo el servidor en `server.py`; no añadir modulos ni capas.
- Imports siempre arriba del fichero, nunca dentro de funciones.
- Estado: nunca tocar `state.json` a mano; mutarlo con `state_tx()` (bajo `_lock`).
- `config.toml` se escribe atomico con `write_config()` (tmp → validar con
  tomllib → replace, bajo `_lock`). Editar por bloques con `find_block`,
  `set_key`, `insert_block`, `remove_block`: tocan solo lo necesario y
  conservan comentarios y orden (el orden de `[[services]]` es el de arranque).
- Errores al usuario en español; HTTP 400 (dato malo) / 404 (no existe) /
  409 (conflicto con el estado actual).
- No romper el frontend. Las acciones de la UI y las tools MCP comparten la
  misma logica del servidor (las tools llaman a los endpoints).
- Si cambia el formato de `config.toml` o `pyproject.toml`, avisarlo
  explicitamente: el usuario tiene que aplicarlo a mano en el Mac.
- Ficheros con fin de linea LF (`.gitattributes` lo fuerza).

## Git

- Commits sin co-autores y sin ninguna referencia a IA.
- Un commit por bloque logico; mensaje en español, describiendo el cambio.
- Rama: `main`.

## Tests

```
uv sync
uv run pytest
```

`tests/test_e2e.py` crea repos git de usar y tirar y un servidor en un
directorio temporal: no necesita `config.toml` ni repos reales. Necesita git,
node/npm y uv en el PATH. Todo cambio va con su test.

`tests/demo_workspace.py` monta un workspace de juguete para probar la UI a mano.

## Modelo (resumen)

- `[[repos]]` (name, path absoluto, kind python|npm, `copy`, `install`),
  `[[services]]` (name, repo, port, cmd con `{port}`), `[[libraries]]`
  (name, consumers). Un repo sin servicio ni libreria sale como "arranque sin
  configurar".
- Un espacio es un perfil: ramas por repo, o worktrees en `worktrees/<perfil>__<repo>`.
  Los repos que no estan en el espacio corren desde su carpeta original.
- Solo un espacio activo a la vez: sus servicios usan los puertos de la config.
- Una rama solo puede estar abierta en un sitio (limitacion de git worktree).
  Para repos que un espacio solo usa (p. ej. un servicio en desa) existe
  "solo lectura" (`readonly = [...]` en el perfil): worktree con HEAD detached
  en lo ultimo de origin/<rama>, asi varios espacios comparten la rama. Se
  actualiza al preparar/arrancar salvo que tenga cambios o commits propios.
- Entornos fijos (seccion aparte en la UI): un perfil con `env_branch` (p. ej.
  desa = develop). Siempre worktree y todos sus repos en solo lectura: los de sus
  servicios (elegibles; por defecto todos), cada uno en `env_branch` salvo
  excepcion en `[repos]`. Se levantan como un espacio (mismo trabajo, mismos
  puertos, solo uno activo). `/api/state` da `environments`: por repo, commits de
  origin que le faltan (`behind`, con lo ya traido; sin fetch), rama que no esta
  en origin (`missing`) y ficheros cambiados. "Actualizar" (tarjeta) = volver a
  levantar. "Actualizar todos" (`/api/environments/update`, trabajo `__entornos`):
  fetch de cada repo una vez y los worktrees de los entornos parados en lo
  ultimo de origin (los crea si faltan), sin instalar ni arrancar; los
  arrancados no se tocan y uno con cambios locales se avisa.
  No salen en Limpieza (nunca sobran) y un espacio no se convierte en entorno ni
  al reves. MCP: `environments` en devws_status, devws_start_environment,
  devws_stop_environment y devws_update_environments.
- Dependencias de un worktree: solo al arrancarlo o con "instalar", y solo si
  cambian sus lockfiles (sello `worktrees/<perfil>__<repo>.deps`).
- `git fetch --prune` solo al hacer checkout, crear worktree, "Comprobar con
  origin" o pedir el plan de borrado de un espacio.
- Al abrir una rama que ya existe en local (crear su worktree, cambiar de rama o
  checkout en el original) se pone en lo ultimo de origin/<rama> si solo le
  faltan commits (fast-forward). Si tiene commits sin subir y origin tambien ha
  avanzado, se abre tal cual y se avisa. Una rama ya abierta no se toca: el
  pull lo hace el usuario.
- Un trabajo (arrancar/preparar/instalar/parar/comprobar con origin) a la vez, salvo Parar: corta el
  trabajo en curso (mata fetch o instalador, deja de esperar puertos) y despues para.
- Estado de un servicio: up, starting, down, crashed (lo arranco devws y su
  proceso murio; se olvida al arrancar o parar su espacio), other_space (su
  puerto lo tiene el servicio de otro espacio) o external (algo de fuera).
- La UI solo reinicia/para/arranca servicios sueltos del espacio activo y nunca
  con force (force en api_up para los servicios de otro espacio).
- `/api/repos` dice donde esta abierta cada rama (`open`) y la rama real y los
  cambios de cada worktree (`worktrees`): con eso la UI avisa antes de arrancar.
- Limpieza (`/api/cleanup`): por espacio, ultimo uso (`used` en state, reflog y
  ficheros con cambios de sus worktrees, `created_at`), tamaño (du en segundo
  plano) y estado de cada rama: nunca subida, commits sin subir, stash, sin
  mergear, mergeada en su destino (base del perfil o rama por defecto; con
  `git merge-tree`, vale para merge, squash y rebase) o borrada en origin.
  Veredicto: borrable (solo si su rama ya esta mergeada y no se pierde nada;
  las de sus otros repos, mergeadas o sin commits propios), revisar (`STALE_DAYS`
  = 7 sin uso y sin mergear) o en_uso. Tambien para espacios in-place. "Comprobar con origin"
  es un trabajo (`__origin`) que hace fetch de todos los repos.
- devws apunta en state (`branches`) las ramas que crea para cada espacio
  (`new`: desde una base; si no, copia local de una que ya estaba en origin).
  Eliminar un espacio (`/api/cleanup/plan` y luego `/api/cleanup/remove` con lo
  confirmado) borra sus carpetas, logs, .code-workspace y esas ramas: la local
  si no se pierde nada, la de origin solo si es `new` y esta mergeada. Nunca una
  protegida (`PROTECTED_BRANCHES`, la por defecto), de otro espacio, abierta en
  otro sitio o que el usuario no confirmo.

## Sin probar en macOS

- Fallback de psutil cuando `net_connections` da AccessDenied (escaneo por proceso).
- Matar por arbol de procesos en lugar de killpg.
- Limpieza: `du -sk` y `git merge-tree --write-tree` (git >= 2.38; si no, no se
  sabe si una rama con commits propios esta mergeada).

## Pendiente (propuesto, sin hacer)

1. Al quitar un repo de un espacio, borrar su worktree (pedir confirmacion si
   tiene cambios sin commitear). Hoy queda como "worktree suelto" en Limpieza,
   desde donde se borra con su rama.
2. Seguridad: quitar credenciales (`user:token@` en URLs, patrones `glpat-…` y
   similares) de los errores de git y de los logs antes de que lleguen a la UI
   o al MCP. El sitio natural es git_msg() en server.py.
3. Opcional: boton "Actualizar ramas" (git fetch) en el formulario de espacio
   (al abrirlo ya se refresca lo local); clonar node_modules con APFS clone.
