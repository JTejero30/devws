<!-- Copiar a <workspace>/.github/copilot-instructions.md (o pegar en AGENTS.md). -->
# Workspace multi-repo con devws

Este workspace tiene varios repos gestionados por devws (herramientas MCP `devws_*`).

- Antes de tocar codigo, llama a `devws_status` para ver repos, ramas y lo que esta arrancado.
- Si la tarea toca **2 o mas repos**, o es una feature/rama nueva que no debe pisar los repos
  originales, propon un espacio worktree (nombre corto, repos, rama de cada uno y base si la
  rama es nueva). **Pide confirmacion** y llama a `devws_create_workspace`.
- Despues edita **solo** en las rutas de `dirs` que devuelve, nunca en los repos originales.
- Para explorar o leer codigo no crees nada: lee los repos originales. Crear un espacio no
  instala dependencias (se instalan al arrancarlo); para pasar tests en un worktree sin
  arrancarlo, llama antes a `devws_install_deps`.
- Si a mitad de tarea hace falta otro repo, vuelve a llamar a `devws_create_workspace` con el
  mismo nombre y ese repo: se anade al espacio.
- Un cambio pequeno en un solo repo y en su rama actual se hace en el sitio, sin worktree.
- `devws_start` con `force` (para otro espacio arrancado) y `devws_remove_workspace` solo con
  confirmacion explicita del usuario.
