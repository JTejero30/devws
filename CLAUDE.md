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
- Dependencias de un worktree: solo al arrancarlo o con "instalar", y solo si
  cambian sus lockfiles (sello `worktrees/<perfil>__<repo>.deps`).
- `git fetch --prune` solo al hacer checkout o crear worktree.

## Sin probar en macOS

- Fallback de psutil cuando `net_connections` da AccessDenied (escaneo por proceso).
- Matar por arbol de procesos en lugar de killpg.

## Pendiente (propuesto, sin hacer)

1. Al quitar un repo de un espacio, borrar su worktree (pedir confirmacion si
   tiene cambios sin commitear), para que no queden worktrees huerfanos; y
   renombrar el panel de "carpetas de worktree que no pertenecen a ningun
   espacio" a algo mas claro.
2. En el formulario de espacio, avisar en linea cuando la rama elegida ya esta
   abierta en otro sitio, sugiriendo dejar el repo fuera para que corra desde
   el original.
3. Seguridad: quitar credenciales (`user:token@` en URLs, patrones `glpat-…` y
   similares) de los errores de git y de los logs antes de que lleguen a la UI
   o al MCP.
4. Opcional: refrescar ramas al abrir el formulario de espacio (o boton
   "Actualizar ramas"); opcion de rama derivada o detached cuando una rama ya
   esta abierta; clonar node_modules con APFS clone.
