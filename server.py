"""Front local multi-repo: ramas, servicios y perfiles de trabajo."""

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import tomllib
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = ROOT.parent
CONFIG = tomllib.load(open(ROOT / "config.toml", "rb"))
STATE_FILE = ROOT / "state.json"
LOGS_DIR = ROOT / "logs"
PROFILES_DIR = ROOT / "profiles"
WT_DIR = ROOT / "worktrees"
WEB_PORT = CONFIG.get("meta", {}).get("web_port", 5000)

NAME_RE = r"[A-Za-z0-9._-]+"

_lock = threading.RLock()


@asynccontextmanager
async def lifespan(_app):
    # el MCP (al final del fichero) necesita su gestor de sesiones vivo
    async with mcp.session_manager.run():
        yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """Solo peticiones a 127.0.0.1/localhost desde la propia UI: cualquier web
    abierta en el navegador podria si no lanzar POSTs (CSRF / DNS rebinding)."""
    allowed = {f"127.0.0.1:{WEB_PORT}", f"localhost:{WEB_PORT}"}
    origin = request.headers.get("origin")
    if request.headers.get("host") not in allowed or (origin and origin.split("://", 1)[-1] not in allowed):
        return JSONResponse({"detail": "origen no permitido"}, status_code=403)
    return await call_next(request)


def load_state() -> dict:
    # bajo _lock: en Windows el replace de save_state falla (acceso denegado)
    # si otro hilo tiene el fichero abierto leyendo
    with _lock:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    return {"services": {}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


@contextmanager
def state_tx():
    """load ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¾Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¾ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ modificar ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¾Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¾ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ save bajo _lock, para no pisar cambios de otros hilos.
    No hacer dentro nada lento (git, kill, esperas de puerto)."""
    with _lock:
        state = load_state()
        state.setdefault("services", {})
        yield state
        save_state(state)


def check_name(name: str, what: str) -> str:
    if not re.fullmatch(NAME_RE, name) or "__" in name:
        raise HTTPException(400, f"nombre de {what} invalido: {name!r} (usa letras, numeros, _ . - y sin '__')")
    return name


def repos() -> list[dict]:
    return CONFIG.get("repos", [])


def services() -> list[dict]:
    return CONFIG.get("services", [])


def repo_path(name: str) -> Path:
    for r in repos():
        if r["name"] == name:
            return (WORKSPACE_ROOT / Path(r["path"]).expanduser()).resolve()
    raise HTTPException(404, f"repo desconocido: {name}")


def service(name: str) -> dict:
    for s in services():
        if s["name"] == name:
            return s
    raise HTTPException(404, f"servicio desconocido: {name}")


def git(repo_dir: Path, *args: str, timeout: int = 90) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip())
    return res.stdout

def tcp_conns() -> list[tuple[int, int, str]]:
    """(puerto local, pid, estado) de los sockets TCP visibles."""
    try:
        return [(c.laddr.port, c.pid, c.status)
                for c in psutil.net_connections("tcp") if c.laddr and c.pid]
    except psutil.AccessDenied:
        # macOS sin root: solo se pueden ver los sockets de procesos propios
        out = []
        for proc in psutil.process_iter():
            try:
                out += [(c.laddr.port, proc.pid, c.status)
                        for c in proc.net_connections("tcp") if c.laddr]
            except psutil.Error:
                continue
        return out


def port_pids(port: int, conns: list | None = None) -> list[int]:
    conns = tcp_conns() if conns is None else conns
    return sorted({pid for p, pid, st in conns if p == port and st == psutil.CONN_LISTEN})


def port_server_pids(port: int) -> list[int]:
    """Pids que parecen servidores en el puerto pero sin socket LISTEN (p. ej.
    CLOSED de un uvicorn zombi): no aceptan conexiones pero sÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¾Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¾ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â­ bloquean el
    bind (address already in use). Ignora conexiones ESTABLISHED y los pids
    propios de devws."""
    return sorted({pid for p, pid, st in tcp_conns()
                   if p == port and st != psutil.CONN_ESTABLISHED
                   and pid not in (os.getpid(), os.getppid())})


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def pid_alive(pid: int) -> bool:
    # ojo: os.kill(pid, 0) en Windows MATA el proceso; psutil es portable
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def wait_port(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(0.5)
    return False


class CheckoutItem(BaseModel):
    repo: str
    branch: str
    base: str | None = None


class CheckoutReq(BaseModel):
    items: list[CheckoutItem]


class ProfileIn(BaseModel):
    name: str
    repos: dict[str, str]
    services: list[str] = []
    worktree: bool = False
    # rama de partida para las ramas que aun no existen (repo -> rama base)
    bases: dict[str, str] = {}


def repo_status(name: str) -> dict:
    p = repo_path(name)
    current = git(p, "branch", "--show-current").strip()
    dirty = bool(git(p, "status", "--porcelain").strip())
    local = git(p, "branch", "--format=%(refname:short)").split()
    remote = [b.replace("origin/", "").strip()
              for b in git(p, "branch", "-r", "--format=%(refname:short)").split()
              if b.startswith("origin/") and "HEAD" not in b]
    return {
        "name": name,
        "current": current or None,
        "dirty": dirty,
        "branches": sorted(set(local) | set(remote)),
    }


@app.get("/api/repos")
def api_repos():
    out = []
    for r in repos():
        try:
            st = repo_status(r["name"])
            st["kind"] = r["kind"]
            st["startup"] = repo_startup(r["name"])
            st["install"] = install_cmd(r)
            out.append(st)
        except Exception as e:
            out.append({"name": r["name"], "error": str(e)})
    return out


def toml_str(s: str) -> str:
    out = ['"']
    for c in s:
        if c == '"':
            out.append('\\"')
        elif c == "\\":
            out.append("\\\\")
        elif c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        elif ord(c) < 0x20 or ord(c) == 0x7f:
            out.append(f"\\u{ord(c):04X}")
        else:
            out.append(c)
    return "".join(out) + '"'


class ServiceIn(BaseModel):
    name: str
    port: int
    # lista de argumentos, o una linea que se parte como en la shell
    cmd: list[str] | str


class StartupIn(BaseModel):
    """Como se arranca un repo: un servicio, o es una libreria (sus consumidores),
    y como se instalan sus dependencias en un worktree (install; [] = nada)."""
    service: ServiceIn | None = None
    library: list[str] | None = None
    install: list[str] | str | None = None


DEFAULT_INSTALL = {"python": ["uv", "sync"], "npm": ["npm", "install", "--no-audit", "--no-fund"]}


def install_cmd(repo: dict) -> list[str]:
    """Instalador de dependencias del repo: el de config.toml o el de su kind."""
    if "install" in repo:
        return repo["install"]
    return DEFAULT_INSTALL.get(repo.get("kind", "python"), DEFAULT_INSTALL["python"])


def split_cmd(cmd: list[str] | str, what: str) -> list[str]:
    try:
        out = shlex.split(cmd) if isinstance(cmd, str) else cmd
    except ValueError as e:
        raise HTTPException(400, f"{what} invalido: {e}")
    if not all(c.strip() for c in out):
        raise HTTPException(400, f"{what} con argumentos vacios")
    return out


def install_value(kind: str, install: list[str] | str) -> list[str] | None:
    """Valor de install para [[repos]]; None si es el de su kind (no se escribe)."""
    cmd = split_cmd(install, "instalador de dependencias")
    return None if cmd == DEFAULT_INSTALL.get(kind) else cmd


def install_line(kind: str, install: list[str] | str | None) -> str:
    """Linea install = [...] para un [[repos]] nuevo; vacia si es la de su kind."""
    v = None if install is None else install_value(kind, install)
    return "" if v is None else f"install = {toml_value(v)}\n"


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return toml_str(v)
    return "[" + ", ".join(toml_value(x) for x in v) + "]"


class RepoIn(StartupIn):
    name: str
    path: str
    kind: str = "python"


def repo_startup(name: str) -> str | None:
    if any(s["repo"] == name for s in services()):
        return "service"
    if any(l["name"] == name for l in CONFIG.get("libraries", [])):
        return "library"
    return None


def startup_block(repo: str, st: StartupIn) -> tuple[str, str]:
    """Valida el arranque y devuelve (tabla, bloque TOML) para config.toml."""
    if (st.service is None) == (st.library is None):
        raise HTTPException(400, "indica un servicio o que es una libreria (uno de los dos)")
    if st.service is not None:
        s = st.service
        check_name(s.name, "servicio")
        if any(x["name"] == s.name for x in services()):
            raise HTTPException(409, f"servicio ya existe: {s.name}")
        if not 0 < s.port < 65536:
            raise HTTPException(400, f"puerto invalido: {s.port}")
        clash = next((x["name"] for x in services() if x["port"] == s.port), None)
        if clash:
            raise HTTPException(409, f"el puerto {s.port} ya lo usa el servicio {clash}")
        cmd = split_cmd(s.cmd, "comando")
        if not cmd:
            raise HTTPException(400, "falta el comando de arranque")
        return "services", (f"[[services]]\nname = {toml_str(s.name)}\nrepo = {toml_str(repo)}\n"
                            f"port = {s.port}\ncmd = [{', '.join(toml_str(c) for c in cmd)}]\n")
    known = {r["name"] for r in repos()}
    consumers = list(dict.fromkeys(st.library))
    for c in consumers:
        if c == repo:
            raise HTTPException(400, "una libreria no puede consumirse a si misma")
        if c not in known:
            raise HTTPException(404, f"repo desconocido: {c}")
    return "libraries", (f"[[libraries]]\nname = {toml_str(repo)}\n"
                         f"consumers = [{', '.join(toml_str(c) for c in consumers)}]\n")


# --- config.toml: edicion por bloques [[tabla]], siempre bajo _lock ----------

def config_lines() -> list[str]:
    return (ROOT / "config.toml").read_text().splitlines(keepends=True)


def write_config(lines: list[str]) -> None:
    """Escritura atomica: .tmp ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¾Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¾ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ validar con tomllib ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¾Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¾ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ replace ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¾Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¾ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ recargar CONFIG."""
    text = "".join(lines)
    try:
        new = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise HTTPException(400, f"config.toml quedaria invalido: {e}")
    cfg_file = ROOT / "config.toml"
    tmp = cfg_file.with_suffix(".toml.tmp")
    tmp.write_text(text)
    tmp.replace(cfg_file)
    CONFIG.clear()
    CONFIG.update(new)


def block_end(lines: list[str], start: int) -> int:
    """Fin del bloque que empieza en `start`: el siguiente header, sin los
    comentarios y blancos que lo preceden (son del bloque siguiente)."""
    j = next((k for k in range(start + 1, len(lines)) if lines[k].strip().startswith("[")), len(lines))
    while j > start + 1 and (not lines[j - 1].strip() or lines[j - 1].lstrip().startswith("#")):
        j -= 1
    return j


def table_starts(lines: list[str], table: str) -> list[int]:
    return [i for i, l in enumerate(lines) if l.strip() == f"[[{table}]]"]


def insert_block(lines: list[str], table: str, block: str, after: tuple[str, ...] = ()) -> None:
    """Inserta `block` tras el ultimo [[table]] (si no hay, tras el ultimo de
    `after`, o al final): el orden de los [[services]] es el de arranque."""
    pos = len(lines)
    for t in (table, *after):
        starts = table_starts(lines, t)
        if starts:
            pos = block_end(lines, starts[-1])
            break
    if pos and not lines[pos - 1].endswith("\n"):
        lines[pos - 1] += "\n"
    lines[pos:pos] = ["\n", block]


def remove_block(lines: list[str], table: str, name: str) -> bool:
    for s in table_starts(lines, table):
        e = block_end(lines, s)
        try:
            found = tomllib.loads("".join(lines[s + 1:e])).get("name") == name
        except tomllib.TOMLDecodeError:
            continue
        if found:
            if s > 0 and not lines[s - 1].strip():
                s -= 1
            del lines[s:e]
            return True
    return False


def find_block(lines: list[str], table: str, name: str) -> tuple[int, int]:
    for s in table_starts(lines, table):
        e = block_end(lines, s)
        try:
            if tomllib.loads("".join(lines[s + 1:e])).get("name") == name:
                return s, e
        except tomllib.TOMLDecodeError:
            continue
    raise HTTPException(404, f"no se encontro el bloque de {name} en config.toml")


def set_key(lines: list[str], table: str, name: str, key: str, value) -> None:
    """Pone key = value en el bloque [[table]] de `name` (None = quitar la clave).
    Edita solo esa clave: se conservan comentarios y orden del resto del bloque."""
    s, e = find_block(lines, table, name)
    k = next((i for i in range(s + 1, e) if lines[i].split("=")[0].strip() == key), None)
    span = 0
    if k is not None:
        # el valor puede ocupar varias lineas (cmd = [\n ...\n]): hasta que parsee
        span = next((j - k for j in range(k + 1, e + 1)
                     if _parses("".join(lines[k:j]))), 1)
    new = [] if value is None else [f"{key} = {toml_value(value)}\n"]
    if k is not None:
        lines[k:k + span] = new
    elif new:
        if not lines[e - 1].endswith("\n"):
            lines[e - 1] += "\n"
        lines[e:e] = new


def _parses(text: str) -> bool:
    try:
        tomllib.loads(text)
        return True
    except tomllib.TOMLDecodeError:
        return False


def insert_startup(lines: list[str], table: str, block: str) -> None:
    insert_block(lines, table, block, after=("repos",) if table == "services" else ())


@app.post("/api/repos")
def api_repos_add(body: RepoIn):
    name = body.name.strip()
    if not re.fullmatch(NAME_RE, name):
        raise HTTPException(400, "nombre invalido (usa letras, numeros, _ . -)")
    path = body.path.strip()
    if not path:
        raise HTTPException(400, "falta el path")
    kind = body.kind if body.kind in ("python", "npm") else "python"
    if any(r["name"] == name for r in repos()):
        raise HTTPException(409, f"repo ya existe: {name}")
    # path absoluto (se admite ~); en config.toml se guarda ya resuelto. Los
    # relativos que ya hubiera en config.toml siguen valiendo (repo_path)
    d = Path(path).expanduser()
    if not d.is_absolute():
        raise HTTPException(400, f"el path tiene que ser absoluto (p. ej. {Path.home() / 'Proyectos' / 'mi-repo'})")
    d = d.resolve()
    if not (d / ".git").exists():
        raise HTTPException(400, f"no hay repo git en {d}")
    # sin arranque tambien se anade: la UI lo marca como pendiente de configurar
    startup = None
    if body.service is not None or body.library is not None:
        startup = startup_block(name, body)
    install = install_line(kind, body.install)
    with _lock:
        lines = config_lines()
        insert_block(lines, "repos", f"[[repos]]\nname = {toml_str(name)}\npath = {toml_str(d.as_posix())}\n"
                                     f"kind = {toml_str(kind)}\n{install}")
        if startup:
            insert_startup(lines, *startup)
        write_config(lines)
    return {"ok": True, "name": name, "startup": repo_startup(name)}


@app.post("/api/repos/{name}/startup")
def api_repos_startup(name: str, body: StartupIn):
    """Configura el arranque de un repo que aun no lo tiene."""
    if name not in {r["name"] for r in repos()}:
        raise HTTPException(404, f"repo desconocido: {name}")
    if repo_startup(name):
        raise HTTPException(409, f"{name} ya tiene arranque configurado (edita config.toml para cambiarlo)")
    table, block = startup_block(name, body)
    repo = next(r for r in repos() if r["name"] == name)
    with _lock:
        lines = config_lines()
        if body.install is not None:
            set_key(lines, "repos", name, "install", install_value(repo.get("kind", "python"), body.install))
        insert_startup(lines, table, block)
        write_config(lines)
    return {"ok": True, "name": name, "startup": repo_startup(name)}


def repo_config(name: str) -> dict:
    """Configuracion completa de un repo tal y como la usa devws."""
    r = next((r for r in repos() if r["name"] == name), None)
    if r is None:
        raise HTTPException(404, f"repo desconocido: {name}")
    lib = next((l for l in CONFIG.get("libraries", []) if l["name"] == name), None)
    p = repo_path(name)
    return {
        "name": name,
        "path": r["path"],
        "resolved_path": str(p),
        "kind": r.get("kind", "python"),
        "install": install_cmd(r),
        "install_is_default": "install" not in r,
        "copy": r.get("copy", []),
        "has_dotenv": (p / ".env").is_file(),
        "services": [{"name": s["name"], "port": s["port"], "cmd": s["cmd"]}
                     for s in services() if s["repo"] == name],
        "library": lib.get("consumers", []) if lib else None,
        "worktrees": sorted(prof for prof, w in wt_profiles().items() if name in w["repos"]),
    }


@app.get("/api/repos/{name}/config")
def api_repos_config(name: str):
    return repo_config(name)


class RepoUpdate(BaseModel):
    """Campos a cambiar de un repo (los que no vienen se quedan como estan).
    services: los servicios del repo por nombre (no se renombran)."""
    path: str | None = None
    kind: str | None = None
    install: list[str] | str | None = None
    copy_files: list[str] | None = None  # clave copy de config.toml
    services: list[ServiceIn] | None = None
    library: list[str] | None = None


def running_services_of(name: str) -> list[str]:
    own = [s["name"] for s in services() if s["repo"] == name]
    return [sid for sid, e in load_state()["services"].items()
            if e and pid_alive(e["pid"]) and sid.split("/")[-1] in own]


@app.put("/api/repos/{name}")
def api_repos_update(name: str, body: RepoUpdate):
    cur = repo_config(name)
    kind = body.kind if body.kind is not None else cur["kind"]
    if kind not in DEFAULT_INSTALL:
        raise HTTPException(400, f"tipo invalido: {kind} (python o npm)")
    edits: list[tuple[str, str, str, object]] = []  # (tabla, nombre, clave, valor)
    if body.path is not None and body.path.strip() != cur["path"]:
        d = Path(body.path.strip()).expanduser()
        if not d.is_absolute():
            raise HTTPException(400, "el path tiene que ser absoluto")
        d = d.resolve()
        if not (d / ".git").exists():
            raise HTTPException(400, f"no hay repo git en {d}")
        if cur["worktrees"] and d != Path(cur["resolved_path"]):
            raise HTTPException(409, f"tiene worktrees en {', '.join(cur['worktrees'])}: "
                                     "borralos antes de cambiar su carpeta")
        edits.append(("repos", name, "path", d.as_posix()))
    if body.kind is not None and kind != cur["kind"]:
        edits.append(("repos", name, "kind", kind))
    if body.install is not None:
        edits.append(("repos", name, "install", install_value(kind, body.install)))
    elif body.kind is not None and cur["install_is_default"] is False and cur["install"] == DEFAULT_INSTALL.get(kind):
        edits.append(("repos", name, "install", None))
    if body.copy_files is not None:
        copy = [c.strip() for c in body.copy_files if c.strip()]
        edits.append(("repos", name, "copy", copy or None))
    if body.services is not None:
        mine = {s["name"]: s for s in cur["services"]}
        for sv in body.services:
            if sv.name not in mine:
                raise HTTPException(404, f"{name} no tiene el servicio {sv.name}")
            if not 0 < sv.port < 65536:
                raise HTTPException(400, f"puerto invalido: {sv.port}")
            clash = next((x["name"] for x in services() if x["port"] == sv.port and x["name"] != sv.name), None)
            if clash:
                raise HTTPException(409, f"el puerto {sv.port} ya lo usa el servicio {clash}")
            cmd = split_cmd(sv.cmd, "comando")
            if not cmd:
                raise HTTPException(400, f"{sv.name}: falta el comando de arranque")
            edits += [("services", sv.name, "port", sv.port), ("services", sv.name, "cmd", cmd)]
    if body.library is not None:
        if cur["library"] is None:
            raise HTTPException(409, f"{name} no es una libreria")
        known = {r["name"] for r in repos()}
        bad = [c for c in body.library if c not in known or c == name]
        if bad:
            raise HTTPException(400, f"consumidores invalidos: {', '.join(bad)}")
        edits.append(("libraries", name, "consumers", list(dict.fromkeys(body.library))))
    with _lock:
        lines = config_lines()
        for table, n, key, value in edits:
            set_key(lines, table, n, key, value)
        write_config(lines)
    out = repo_config(name)
    if running_services_of(name) and body.services:
        out["warn"] = "los servicios arrancados siguen con la config anterior hasta relanzarlos"
    return out


@app.delete("/api/repos/{name}/startup")
def api_repos_startup_del(name: str):
    """Quita el arranque del repo (sus servicios o su bloque de libreria): queda
    pendiente de configurar. Los perfiles que tuvieran esos servicios los ignoran."""
    cur = repo_config(name)
    running = running_services_of(name)
    if running:
        raise HTTPException(409, f"tiene servicios arrancados: {', '.join(running)}; paralos antes")
    with _lock:
        lines = config_lines()
        for s in cur["services"]:
            remove_block(lines, "services", s["name"])
        remove_block(lines, "libraries", name)
        write_config(lines)
    return repo_config(name)


@app.delete("/api/repos/{name}")
def api_repos_del(name: str):
    if name not in {r["name"] for r in repos()}:
        raise HTTPException(404, "repo desconocido")
    # se quita con su arranque (sus [[services]] y su [[libraries]]), salvo si corre
    own = [s["name"] for s in services() if s["repo"] == name]
    running = running_services_of(name)
    if running:
        raise HTTPException(409, f"tiene servicios arrancados: {', '.join(running)}; paralos antes")
    libs = [l["name"] for l in CONFIG.get("libraries", []) if name in l.get("consumers", [])]
    if libs:
        raise HTTPException(409, f"consume la(s) libreria(s) {', '.join(libs)}: quitalo de alli antes")
    used = {prof for prof, w in wt_profiles().items() if name in w["repos"]}
    if used:
        raise HTTPException(409, f"en uso por perfil(s): {', '.join(sorted(used))}")
    with _lock:
        lines = config_lines()
        if not remove_block(lines, "repos", name):
            raise HTTPException(404, "no se encontro el bloque del repo en config.toml")
        for s in own:
            remove_block(lines, "services", s)
        remove_block(lines, "libraries", name)
        write_config(lines)
    return {"ok": True, "name": name}


def load_dotenv(repo_dir: Path) -> dict:
    f = repo_dir / ".env"
    env = {}
    if not f.exists():
        return env
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def ps_command(pid: int) -> str:
    try:
        return " ".join(psutil.Process(pid).cmdline())
    except psutil.Error:
        return ""


DEV_PROC_MARKERS = ("uvicorn", "node", "ng", "npm", "python", "fastapi")


def is_dev_proc(pid: int) -> bool:
    """Proceso de desarrollo si algun ejecutable/script de su cmdline se llama
    como un marcador (antes se buscaba subcadena: "ng" casaba con "settings")."""
    try:
        cmdline = psutil.Process(pid).cmdline()
    except psutil.Error:
        return False
    for tok in cmdline:
        base = Path(tok).name.lower().removesuffix(".exe").removesuffix(".cmd")
        if base in DEV_PROC_MARKERS or base.startswith(("python", "uvicorn")):
            return True
    return False

# "address already in use" en macOS (48), Linux (98) y Windows (10048)
ADDR_IN_USE = ("Errno 48", "Errno 98", "10048", "address already in use")


def kill_process(pid: int) -> None:
    """SIGTERM al proceso y a todos sus descendientes; SIGKILL a los que sigan vivos."""
    try:
        root = psutil.Process(pid)
        procs = [root, *root.children(recursive=True)]
    except psutil.Error:
        return
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=5)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=5)


def wait_port_free(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_open(port):
            return True
        time.sleep(0.5)
    return not port_open(port)


def _ref_exists(repo_dir: Path, ref: str) -> bool:
    res = subprocess.run(
        ["git", "-C", str(repo_dir), "show-ref", "--verify", "--quiet", ref],
        capture_output=True, text=True,
    )
    return res.returncode == 0


def resolve_base(p: Path, name: str, branch: str, base: str | None) -> str:
    """Punto de partida de una rama nueva: origin/<base> (recien traida) o la
    rama local <base>; sin base, la rama por defecto de origin. Nunca el HEAD
    que tenga en ese momento el repo original."""
    if base:
        if _ref_exists(p, f"refs/remotes/origin/{base}"):
            return f"origin/{base}"
        if _ref_exists(p, f"refs/heads/{base}"):
            return base
        raise HTTPException(400, f"{name}: no existe la rama base {base}")
    if _ref_exists(p, "refs/remotes/origin/HEAD"):
        return "origin/HEAD"
    raise HTTPException(400, f"{name}: {branch} no existe; indica desde que rama crearla")


def branch_start(p: Path, name: str, branch: str, base: str | None) -> tuple[list[str], str]:
    """(opciones, punto de partida) para checkout / worktree add de `branch`:
    rama local, rama de origin con tracking, o rama nueva sin tracking."""
    if _ref_exists(p, f"refs/heads/{branch}"):
        return [], branch
    if _ref_exists(p, f"refs/remotes/origin/{branch}"):
        return ["-b", branch], f"origin/{branch}"
    return ["--no-track", "-b", branch], resolve_base(p, name, branch, base)


def branch_open_at(p: Path, branch: str, exclude: Path | None = None) -> str | None:
    """Ruta del working tree (repo original u otro worktree) que tiene `branch`."""
    path = None
    for line in git(p, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line.removeprefix("worktree ")
        elif line == f"branch refs/heads/{branch}" and not (exclude and Path(path).resolve() == exclude.resolve()):
            return path
    return None


def do_checkout(name: str, branch: str, base: str | None = None) -> str:
    p = repo_path(name)
    current = git(p, "branch", "--show-current").strip()
    if current == branch:
        return branch
    git(p, "fetch", "--prune", "--quiet", timeout=120)
    if git(p, "status", "--porcelain").strip():
        raise HTTPException(409, f"{name}: working tree dirty, no se hace checkout")
    opts, start = branch_start(p, name, branch, base)
    git(p, "checkout", "-q", *opts, start)
    return branch


def _split_svc_id(svc_id: str) -> tuple[str | None, str]:
    if "/" in svc_id:
        prof, name = svc_id.split("/", 1)
        return prof, name
    return None, svc_id


def log_file_for(svc_id: str) -> Path:
    prof, name = _split_svc_id(svc_id)
    return LOGS_DIR / (f"{prof}--{name}.log" if prof else f"{name}.log")


def running_of_other_space(state: dict, my_prof: str | None) -> list[str]:
    """Servicios corriendo de OTRO espacio de trabajo (perfil distinto o base vs wt)."""
    out = []
    for name, entry in state.get("services", {}).items():
        if not entry or not pid_alive(entry["pid"]):
            continue
        prof, _ = _split_svc_id(name)
        if prof != my_prof:
            out.append(name)
    return out


def wt_link_github(name: str, d: Path) -> None:
    """.github (instrucciones de Copilot) esta en .gitignore: el worktree no lo
    materializa; enlazarlo desde el repo original para que siempre se vean."""
    src = repo_path(name) / ".github"
    dst = d / ".github"
    if src.is_dir() and not dst.exists() and not dst.is_symlink():
        try:
            dst.symlink_to(src)
        except OSError:
            pass


def wt_copy_ignored(name: str, d: Path) -> None:
    """Copia al worktree los ficheros ignorados por git que el repo declara en
    `copy` (config.toml). Nunca sobrescribe: el worktree puede tenerlos editados."""
    cfg = next((r for r in repos() if r["name"] == name), {})
    src_root = repo_path(name)
    for rel in cfg.get("copy", []):
        src, dst = src_root / rel, d / rel
        if not src.exists() or dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def wt_dir_for(profile: str, repo: str) -> Path:
    return WT_DIR / f"{profile}__{repo}"


def wt_ensure(name: str, branch: str, profile: str, base: str | None = None) -> Path:
    """Deja el worktree del repo para el perfil en `branch` (lo crea si no existe).
    Si la rama ya esta abierta en otro working tree falla: git no permite la
    misma rama en dos sitios y forzarlo (--force) acaba en lios."""
    p = repo_path(name)
    d = wt_dir_for(profile, name)
    git(p, "fetch", "--prune", "--quiet", timeout=120)
    if d.exists():
        cur = git(d, "branch", "--show-current").strip()
        if cur != branch:
            if git(d, "status", "--porcelain").strip():
                raise HTTPException(409, f"{name}: worktree con cambios locales; resuelve antes de pasar a {branch}")
            other = branch_open_at(p, branch, exclude=d)
            if other:
                raise HTTPException(409, f"{name}: la rama {branch} ya esta abierta en {other}")
            opts, start = branch_start(p, name, branch, base)
            git(d, "checkout", "-q", *opts, start)
    else:
        git(p, "worktree", "prune")
        other = branch_open_at(p, branch)
        if other:
            raise HTTPException(409, f"{name}: la rama {branch} ya esta abierta en {other}; "
                                     "usa otra rama o cambia la de alli")
        opts, start = branch_start(p, name, branch, base)
        git(p, "worktree", "add", "-q", *opts, str(d), start, timeout=300)
    wt_link_github(name, d)
    wt_copy_ignored(name, d)
    return d


@app.post("/api/checkout")
def api_checkout(req: CheckoutReq):
    results = []
    for item in req.items:
        try:
            do_checkout(item.repo, item.branch, item.base)
            results.append({"repo": item.repo, "ok": True, "branch": item.branch})
        except (HTTPException, RuntimeError) as e:
            results.append({"repo": item.repo, "ok": False,
                            "error": str(e.detail) if isinstance(e, HTTPException) else str(e)})
    libs = {l["name"]: l.get("consumers", []) for l in CONFIG.get("libraries", [])}
    for item in req.items:
        if item.repo in libs and any(r["repo"] == item.repo and r["ok"] for r in results):
            targets = [item.repo, *libs[item.repo]]
            threading.Thread(target=uv_sync_many, args=(targets,), daemon=True).start()
    return results


def uv_sync_many(targets: list[str]) -> None:
    for name in targets:
        try:
            p = repo_path(name)
            subprocess.run(["uv", "sync"], cwd=p, capture_output=True, text=True, timeout=600)
        except Exception:
            pass


def _svc_ctx(name: str):
    """Servicio, perfil, puerto, directorio de trabajo y worktree (None si el
    servicio corre desde el repo original: su repo no esta en el perfil)."""
    svc = service(name.split("/")[-1])
    prof, _ = _split_svc_id(name)
    p = repo_path(svc["repo"])
    wt_dir = wt_dir_for(prof, svc["repo"]) if prof else None
    if wt_dir is not None and wt_dir.is_dir():
        p = wt_dir
    else:
        wt_dir = None
    return svc, prof, svc["port"], p, wt_dir


@app.post("/api/services/{name}/up")
def api_up(name: str, force: bool = False):
    svc, prof, port, p, wt_dir = _svc_ctx(name)
    if wt_dir is not None and deps_missing(svc["repo"], wt_dir):
        return {"ok": False, "error": "deps",
                "msg": f"{svc['repo']}: el worktree no tiene dependencias; arranca el espacio para instalarlas"}
    state = load_state()
    entry = state["services"].get(name)
    if entry and pid_alive(entry["pid"]) and not force:
        return {"ok": True, "msg": "ya estaba corriendo", "pid": entry["pid"]}
    others = running_of_other_space(state, prof)
    if others:
        if not force:
            raise HTTPException(409, "un solo espacio de trabajo a la vez: "
                                f"corren servicios de otro espacio ({', '.join(others)}). "
                                "BÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¾Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¾ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡jalos antes (o usa force=true para cambiar de espacio)")
        with state_tx() as st:
            gone = [st["services"].pop(oid, None) for oid in others]
        for oentry in gone:
            if oentry:
                kill_process(oentry["pid"])
    killed = []
    port_pids_now = port_pids(port)
    if port_pids_now:
        blockers = []
        for pid in port_pids_now:
            cmd = ps_command(pid)
            if is_dev_proc(pid):
                kill_process(pid)
                killed.append((pid, cmd[:60]))
            elif not force:
                blockers.append((pid, cmd[:60]))
        if blockers:
            return {"ok": False, "error": "port_conflict",
                    "msg": f"puerto {port} ocupado por proceso no dev: "
                           f"{[(x, c) for x, c in blockers]}"}
        if not wait_port_free(port):
            return {"ok": False, "error": "port_conflict",
                    "msg": f"el puerto {port} sigue ocupado tras matar {killed}"}
    # sin listener pero con sockets zombi (p. ej. CLOSED de un uvicorn --reload
    # anterior): no responde a connect, pero bloquea el bind con Errno 48
    zombis = [p for p in port_server_pids(port) if is_dev_proc(p)]
    for pid in zombis:
        kill_process(pid)
    if zombis and not wait_port_free(port):
        return {"ok": False, "error": "port_conflict",
                "msg": f"el puerto {port} sigue bloqueado por pids {zombis} tras matarlos"}
    return _start_service(svc, port, killed, name=name, wt_dir=wt_dir, path=p)


def log_tail(svc_id: str, chars: int = 4000) -> str:
    f = log_file_for(svc_id)
    if not f.exists():
        return ""
    return f.read_bytes()[-chars:].decode(errors="replace")


def stray_pids(repo_name: str, exclude: set[int], wt_dir: Path | None = None) -> list[int]:
    needle = str(wt_dir) if wt_dir else repo_name
    out = []
    for proc in psutil.process_iter(["cmdline"]):
        cmd = " ".join(proc.info["cmdline"] or [])
        if proc.pid in exclude or "uvicorn" not in cmd or needle not in cmd:
            continue
        out.append(proc.pid)
    return out


def _start_service(svc: dict, port: int, killed: list,
                   name: str | None = None, wt_dir: Path | None = None,
                   path: Path | None = None) -> dict:
    name = name or svc["name"]
    p = path or repo_path(svc["repo"])
    (LOGS_DIR).mkdir(exist_ok=True)
    env = os.environ.copy()
    # .env esta en .gitignore: el worktree no lo tiene; usar el del repo original
    dotenv_p = repo_path(svc["repo"]) if wt_dir is not None else p
    for k, v in load_dotenv(dotenv_p).items():
        env.setdefault(k, v)
    exclude = {os.getpid()}
    logf_path = log_file_for(name)
    cmd = [a.replace("{port}", str(port)) for a in svc["cmd"]]
    # ejecutable relativo al repo (./.venv/bin/python): en macOS/Linux Popen lo
    # resuelve contra cwd, en Windows contra el cwd de devws; fijarlo al repo
    if not Path(cmd[0]).is_absolute() and ("/" in cmd[0] or "\\" in cmd[0]):
        cmd[0] = str(p / cmd[0])
    for attempt in (1, 2):
        if attempt == 1:
            with open(logf_path, "wb") as f:
                f.write(f"=== up {name} {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
        with open(logf_path, "ab") as logf:
            proc = subprocess.Popen(
                cmd, cwd=p, stdout=logf, stderr=subprocess.STDOUT,
                start_new_session=True, env=env,
            )
        try:
            branch = git(p, "branch", "--show-current", timeout=10).strip()
        except Exception:
            branch = ""
        entry = {"pid": proc.pid, "port": port,
                 "started_at": time.strftime("%H:%M:%S"),
                 "branch": branch}
        if wt_dir is not None:
            entry["wt"] = wt_dir.name
        with state_tx() as st:
            st["services"][name] = entry
        if wait_port(port, timeout=90):
            extra = f" (matado {len(killed)} proceso(s) previo(s))" if killed else ""
            return {"ok": True, "msg": f"up en :{port}{extra}", "pid": proc.pid}
        tail = log_tail(name)
        exclude.add(proc.pid)
        if attempt == 1 and any(m in tail for m in ADDR_IN_USE):
            strays = stray_pids(svc["repo"], exclude, wt_dir)
            for pid in strays:
                kill_process(pid)
            time.sleep(1)
            continue
        return {"ok": True, "warn": f"proceso lanzado (pid {proc.pid}) pero el puerto {port} "
                "no respondio a tiempo; revisa el log"}
    return {"ok": True, "warn": f"el puerto {port} sigue sin responder; revisa el log"}


@app.post("/api/services/{name}/down")
def api_down(name: str):
    service(name.split("/")[-1])
    with state_tx() as st:
        entry = st["services"].pop(name, None)
    if not entry:
        return {"ok": True, "msg": "no estaba gestionado por devws"}
    kill_process(entry["pid"])
    with state_tx() as st:
        remaining = any(e and pid_alive(e["pid"]) for e in st["services"].values())
        if not remaining and st.get("active_profile"):
            st["active_profile"] = None
    return {"ok": True, "msg": "down"}


def service_status(name: str, svc: dict, state: dict, conns: list | None = None) -> dict:
    entry = state["services"].get(name)
    prof, _ = _split_svc_id(name)
    port = svc["port"]
    _, _, _, p, wt_dir = _svc_ctx(name)
    pids = port_pids(port, conns)
    if entry and pid_alive(entry["pid"]) and pids:
        status = "up"
    elif entry and pid_alive(entry["pid"]):
        status = "starting"
    elif pids:
        status = "external"
    else:
        status = "down"
    out = {"id": name, "name": svc["name"], "repo": svc["repo"], "port": port,
           "status": status, "pid": entry["pid"] if entry else None,
           "branch": entry.get("branch") if entry else None,
           "port_pids": pids}
    if prof:
        out["wt"] = True
        out["profile"] = prof
        out["from_original"] = wt_dir is None
    try:
        out["repo_branch"] = git(p, "branch", "--show-current", timeout=5).strip() or None
    except Exception:
        out["repo_branch"] = None
    return out


def service_ids(state: dict, wts: dict) -> list[str]:
    """Servicios base + los de cada perfil worktree (solo los del perfil) + los
    que sigan corriendo aunque ya no esten en su perfil (para poder bajarlos)."""
    ids = [s["name"] for s in services()]
    for prof, w in wts.items():
        ids += [f"{prof}/{s}" for s in w["services"]]
    known = {s["name"] for s in services()}
    ids += [i for i in state["services"] if i not in ids and i.split("/")[-1] in known]
    return ids


@app.get("/api/state")
def api_state():
    state = load_state()
    wts = wt_profiles()
    for prof, w in wts.items():
        w["pending"] = wt_pending(prof)
    conns = tcp_conns()
    return {"services": [service_status(i, service(i.split("/")[-1]), state, conns)
                         for i in service_ids(state, wts)],
            "active_profile": state.get("active_profile"),
            "worktrees": wts,
            "jobs": state.get("jobs", {})}


@app.get("/api/logs/tail")
def api_logs_tail(lines: int = 200):
    ids = service_ids(load_state(), wt_profiles())
    events = []
    for i in ids:
        f = log_file_for(i)
        if not f.exists():
            continue
        content = f.read_bytes().decode(errors="replace").splitlines()
        events.extend({"svc": i, "line": l} for l in content[-lines:])
    return events


@app.get("/api/logs/{name}")
def api_logs(name: str, lines: int = 150):
    f = log_file_for(name)
    if not f.exists():
        return {"lines": []}
    content = f.read_bytes().decode(errors="replace").splitlines()
    return {"lines": content[-lines:]}


@app.get("/api/profiles")
def api_profiles_list():
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.toml"))


@app.get("/api/profiles/{name}")
def api_profile_get(name: str):
    prof = load_profile(name)
    if prof is None:
        raise HTTPException(404, f"perfil no existe: {name}")
    return prof


def profile_file(name: str) -> Path:
    return PROFILES_DIR / f"{name}.toml"


def load_profile(name: str) -> dict | None:
    f = profile_file(name)
    if not f.exists():
        return None
    return tomllib.loads(f.read_text())


def profile_services(prof: dict) -> list[str]:
    """Servicios del perfil en su orden de arranque (lista vacia = todos)."""
    known = [s["name"] for s in services()]
    wanted = prof.get("services", {}).get("services") or known
    return [s for s in wanted if s in known]


def profile_body(req: ProfileIn, created_at: str | None = None) -> str:
    body = [
        f'name = {toml_str(req.name)}',
        f'created_at = {toml_str(created_at or time.strftime("%Y-%m-%d %H:%M"))}',
        f'worktree = {"true" if req.worktree else "false"}',
        "", "[repos]",
    ]
    body += [f'{toml_str(r)} = {toml_str(b)}' for r, b in req.repos.items()]
    bases = {r: b for r, b in req.bases.items() if r in req.repos and b}
    if bases:
        body += ["", "[bases]"] + [f'{toml_str(r)} = {toml_str(b)}' for r, b in bases.items()]
    body += ["", "[services]", "services = [" + ", ".join(toml_str(s) for s in req.services) + "]"]
    return "\n".join(body) + "\n"


def profile_in(name: str, prof: dict) -> ProfileIn:
    return ProfileIn(name=name, repos=prof.get("repos", {}), worktree=bool(prof.get("worktree")),
                     services=prof.get("services", {}).get("services", []),
                     bases=prof.get("bases", {}))


@app.post("/api/profiles")
def api_profiles_save(req: ProfileIn):
    check_name(req.name, "perfil")
    PROFILES_DIR.mkdir(exist_ok=True)
    f = profile_file(req.name)
    if f.exists():
        raise HTTPException(409, f"el perfil {req.name} ya existe; usa PUT para actualizarlo")
    f.write_text(profile_body(req))
    return {"ok": True}


@app.put("/api/profiles")
def api_profiles_update(req: ProfileIn):
    check_name(req.name, "perfil")
    old = load_profile(req.name)
    if old is None:
        raise HTTPException(404, f"perfil no existe: {req.name}")
    profile_file(req.name).write_text(profile_body(req, old.get("created_at")))
    return {"ok": True}


@app.delete("/api/profiles/{name}")
def api_profiles_delete(name: str):
    f = profile_file(check_name(name, "perfil"))
    if f.exists():
        f.unlink()
    with state_tx() as st:
        if st.get("active_profile") == name:
            st["active_profile"] = None
        st.get("jobs", {}).pop(name, None)
    return {"ok": True}


# --- espacios de trabajo: arrancar / parar perfiles --------------------------
# Un perfil es un espacio de trabajo: in-place (checkout en los repos originales)
# o worktree (worktrees/{perfil}__{repo}; los repos no incluidos corren desde el
# original). Solo uno arrancado a la vez. Arrancar y parar son trabajos en
# segundo plano: progreso en state["jobs"][perfil] = {action, status, step, error}
# y salida en logs/{perfil}--actividad.log. De los worktrees, la fuente de verdad
# es el perfil (profiles/*.toml) + lo que hay en disco.


def wt_profiles() -> dict[str, dict]:
    """Perfiles worktree con algun worktree en disco."""
    out = {}
    if not PROFILES_DIR.exists():
        return out
    for f in sorted(PROFILES_DIR.glob("*.toml")):
        try:
            prof = tomllib.loads(f.read_text())
        except tomllib.TOMLDecodeError:
            continue
        if not prof.get("worktree"):
            continue
        on_disk = {r: b for r, b in prof.get("repos", {}).items() if wt_dir_for(f.stem, r).is_dir()}
        if on_disk:
            out[f.stem] = {"repos": on_disk, "services": profile_services(prof)}
    return out


def wt_pending(profile: str) -> list[str]:
    """Repos del perfil cuyo worktree falta o esta en otra rama (se aplica al arrancar)."""
    out = []
    for r, b in (load_profile(profile) or {}).get("repos", {}).items():
        d = wt_dir_for(profile, r)
        try:
            if not d.is_dir() or git(d, "branch", "--show-current", timeout=5).strip() != b:
                out.append(r)
        except Exception:
            out.append(r)
    return out


def wt_dirty(d: Path) -> int:
    """Ficheros con cambios sin commitear (incluye no versionados no ignorados)."""
    try:
        return len(git(d, "status", "--porcelain").splitlines())
    except RuntimeError:
        return 0


def space_ids(profile: str, prof: dict, state: dict) -> list[str]:
    """Ids de servicio corriendo que pertenecen al espacio del perfil."""
    alive = [sid for sid, e in state["services"].items() if e and pid_alive(e["pid"])]
    if prof.get("worktree"):
        return [sid for sid in alive if sid.startswith(f"{profile}/")]
    if state.get("active_profile") == profile:
        return [sid for sid in alive if "/" not in sid]
    return []


def job_log(profile: str) -> Path:
    return log_file_for(f"{profile}--actividad")


def _set_job(profile: str, **info) -> None:
    with state_tx() as st:
        st.setdefault("jobs", {})[profile] = info


class Job:
    """Contexto de un trabajo: anota pasos en state y en el log de actividad."""

    def __init__(self, profile: str, action: str):
        self.profile, self.action = profile, action
        LOGS_DIR.mkdir(exist_ok=True)
        self.log = job_log(profile)
        self.log.write_text(f"=== {action} {profile} {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")

    def step(self, msg: str) -> None:
        _set_job(self.profile, action=self.action, status="running", step=msg)
        with open(self.log, "a") as fh:
            fh.write(f"\n--- {time.strftime('%H:%M:%S')} {msg}\n")

    def run(self, cmd: list[str], cwd: Path, timeout: int) -> int:
        with open(self.log, "ab") as fh:
            return subprocess.run([shutil.which(cmd[0]) or cmd[0], *cmd[1:]], cwd=cwd,
                                  stdout=fh, stderr=subprocess.STDOUT, timeout=timeout).returncode


def deps_install(job: Job, rname: str, d: Path) -> None:
    r = next((r for r in repos() if r["name"] == rname), {})
    cmd = install_cmd(r)
    if not cmd:
        return
    job.step(f"{rname}: instalando dependencias ({' '.join(cmd[:2])})")
    rc = job.run(cmd, d, 1800 if r.get("kind") == "npm" else 900)
    if rc != 0 and cmd == DEFAULT_INSTALL["python"]:
        # registry de paquetes caido: reintentar con la cache local (wheels de otros repos)
        job.step(f"{rname}: uv sync --offline")
        rc = job.run(["uv", "sync", "--offline"], d, 900)
    if rc != 0:
        raise RuntimeError(f"{rname}: fallo la instalacion de dependencias (codigo {rc})")
    # despues de instalar: uv sync / npm install pueden crear o tocar el lockfile
    deps_stamp_file(d).write_text(deps_hash(rname, d))


# ficheros que definen las dependencias: si no cambian, no se reinstala
DEPS_FILES = ("uv.lock", "pyproject.toml", "requirements.txt", "package-lock.json", "package.json")


def deps_stamp_file(d: Path) -> Path:
    # junto al worktree, no dentro: dentro saldria como fichero sin versionar
    return d.with_name(d.name + ".deps")


def deps_hash(rname: str, d: Path) -> str:
    r = next((r for r in repos() if r["name"] == rname), {})
    h = hashlib.sha256(json.dumps(install_cmd(r)).encode())
    for f in DEPS_FILES:
        if (d / f).is_file():
            h.update(f.encode() + b"\0" + (d / f).read_bytes())
    return h.hexdigest()


def deps_needed(rname: str, d: Path) -> bool:
    """Faltan las dependencias o cambiaron sus ficheros desde la ultima instalacion."""
    if deps_missing(rname, d):
        return True
    r = next((r for r in repos() if r["name"] == rname), {})
    if not install_cmd(r):
        return False
    f = deps_stamp_file(d)
    return not f.exists() or f.read_text().strip() != deps_hash(rname, d)


def deps_missing(rname: str, d: Path) -> bool:
    r = next((r for r in repos() if r["name"] == rname), {})
    if not install_cmd(r):
        return False
    return not (d / ("node_modules" if r.get("kind") == "npm" else ".venv")).exists()


def job_prepare(job: Job, prof: dict, deps: bool = False) -> None:
    """Worktrees en su rama. Con deps, tambien sus dependencias, y solo si
    cambiaron los ficheros que las definen (lockfiles), no por cambiar de rama.
    Para explorar o editar no hacen falta: se instalan al arrancar."""
    bases = prof.get("bases", {})
    for rname, branch in prof.get("repos", {}).items():
        d = wt_dir_for(job.profile, rname)
        job.step(f"{rname}: worktree en {branch}" if not d.is_dir() else f"{rname}: comprobando worktree")
        wt_ensure(rname, branch, job.profile, bases.get(rname))
    if deps:
        for rname in prof.get("repos", {}):
            d = wt_dir_for(job.profile, rname)
            if deps_needed(rname, d):
                deps_install(job, rname, d)


def job_checkout(job: Job, prof: dict) -> None:
    """In-place: checkout en los repos originales (+ uv sync si cambia una libreria)."""
    bases = prof.get("bases", {})
    libs = {l["name"]: l.get("consumers", []) for l in CONFIG.get("libraries", [])}
    for rname, branch in prof.get("repos", {}).items():
        before = git(repo_path(rname), "branch", "--show-current").strip()
        job.step(f"{rname}: checkout {branch}")
        do_checkout(rname, branch, bases.get(rname))
        if rname in libs and before != branch:
            for target in [rname, *libs[rname]]:
                job.step(f"{target}: uv sync (cambio la libreria {rname})")
                job.run(["uv", "sync"], repo_path(target), 600)


def job_start(job: Job, prof: dict, services_too: bool = True) -> list[str]:
    is_wt = bool(prof.get("worktree"))
    if services_too:
        # regla de oro: un solo espacio arrancado; se para todo lo que corra
        running = [sid for sid, e in load_state()["services"].items() if e and pid_alive(e["pid"])]
        for sid in reversed(running):
            job.step(f"parando {sid} (otro espacio)")
            api_down(sid)
    if is_wt:
        job_prepare(job, prof, deps=services_too or job.action == "install")
    else:
        job_checkout(job, prof)
    if not services_too:
        return []
    errors = []
    svcs = profile_services(prof)
    for i, s in enumerate(svcs, 1):
        job.step(f"arrancando {s} ({i}/{len(svcs)})")
        r = api_up(f"{job.profile}/{s}" if is_wt else s, force=True)
        if not r.get("ok") or r.get("warn"):
            errors.append(f"{s}: {r.get('warn') or r.get('msg')}")
    with state_tx() as st:
        st["active_profile"] = job.profile
    return errors


def job_stop(job: Job, prof: dict) -> None:
    state = load_state()
    ids = space_ids(job.profile, prof, state)
    order = profile_services(prof)
    ids.sort(key=lambda sid: -order.index(sid.split("/")[-1]) if sid.split("/")[-1] in order else 0)
    for sid in ids:
        job.step(f"parando {sid.split('/')[-1]}")
        api_down(sid)
    with state_tx() as st:
        if st.get("active_profile") == job.profile:
            st["active_profile"] = None


def run_job(profile: str, action: str) -> None:
    job = Job(profile, action)
    prof = load_profile(profile) or {}
    errors = []
    try:
        if action == "stop":
            job_stop(job, prof)
        else:
            errors = job_start(job, prof, services_too=(action == "start"))
    except Exception as e:  # hilo en segundo plano: el error tiene que llegar a la UI
        errors.append(str(e.detail) if isinstance(e, HTTPException) else str(e))
    with open(job.log, "a") as fh:
        fh.write("\n=== " + ("ERROR: " + "; ".join(errors) if errors else "listo") + " ===\n")
    _set_job(profile, action=action, status="error" if errors else "ok",
             error="; ".join(errors) or None)


def start_job(profile: str, action: str) -> None:
    with state_tx() as st:
        busy = [p for p, j in st.get("jobs", {}).items() if j.get("status") == "running"]
        if busy:
            raise HTTPException(409, f"espera: {busy[0]} todavia esta trabajando")
        st.setdefault("jobs", {})[profile] = {"action": action, "status": "running",
                                              "step": "parandoÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¾Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¦" if action == "stop" else "empezandoÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¾Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¦"}
    threading.Thread(target=run_job, args=(profile, action), daemon=True).start()


def reset_interrupted_jobs() -> None:
    """Un trabajo 'running' al arrancar el servidor murio con el anterior."""
    with state_tx() as st:
        for info in st.get("jobs", {}).values():
            if info.get("status") == "running":
                info.update(status="error", step=None,
                            error="interrumpido al reiniciar devws; vuelve a intentarlo")


def _profile_or_404(name: str) -> dict:
    prof = load_profile(name)
    if prof is None:
        raise HTTPException(404, f"perfil no existe: {name}")
    for r in prof.get("repos", {}):
        repo_path(r)
    return prof


@app.post("/api/profiles/{name}/start")
def api_profile_start(name: str, force: bool = False):
    """Arranca el espacio del perfil (prepara/checkout + servicios). Si hay otro
    espacio arrancado responde 409 salvo force=true, que lo para antes."""
    prof = _profile_or_404(name)
    state = load_state()
    mine = set(space_ids(name, prof, state))
    others = [sid for sid, e in state["services"].items()
              if e and pid_alive(e["pid"]) and sid not in mine]
    if others and not force:
        owner = state.get("active_profile") or others[0].partition("/")[0]
        raise HTTPException(409, f"esta arrancado {owner}: se parara para arrancar {name}")
    start_job(name, "start")
    return {"ok": True}


@app.post("/api/profiles/{name}/stop")
def api_profile_stop(name: str):
    _profile_or_404(name)
    start_job(name, "stop")
    return {"ok": True}


@app.post("/api/profiles/{name}/prepare")
def api_profile_prepare(name: str):
    """Solo los worktrees en su rama (sin dependencias ni servicios): rapido,
    basta para explorar o editar."""
    if not _profile_or_404(name).get("worktree"):
        raise HTTPException(400, f"{name} no es un perfil worktree")
    start_job(name, "prepare")
    return {"ok": True}


@app.post("/api/profiles/{name}/install")
def api_profile_install(name: str):
    """Worktrees + dependencias (las que hayan cambiado), sin arrancar servicios:
    p. ej. para pasar los tests en el worktree."""
    if not _profile_or_404(name).get("worktree"):
        raise HTTPException(400, f"{name} no es un perfil worktree")
    start_job(name, "install")
    return {"ok": True}


def orphan_worktrees() -> list[dict]:
    registered = {wt_dir_for(prof, r).name for prof, w in wt_profiles().items() for r in w["repos"]}
    out = []
    if not WT_DIR.exists():
        return out
    for d in sorted(WT_DIR.iterdir()):
        if not d.is_dir() or "__" not in d.name or d.name in registered:
            continue
        # los perfiles no pueden llevar "__": el primero separa perfil y repo
        prof, _, rname = d.name.partition("__")
        try:
            branch = git(d, "branch", "--show-current").strip() or None
        except Exception:
            continue
        out.append({"dir": d.name, "profile": prof, "repo": rname,
                    "branch": branch, "dirty": wt_dirty(d) > 0})
    return out


@app.get("/api/worktrees/orphans")
def api_wt_orphans():
    return orphan_worktrees()


@app.post("/api/worktrees/orphans/{dir}/adopt")
def api_wt_adopt(dir: str):
    if "__" not in dir or not (WT_DIR / dir).is_dir():
        raise HTTPException(404, f"worktree no existe: {dir}")
    prof, _, rname = dir.partition("__")
    check_name(prof, "perfil")
    try:
        repo_path(rname)
    except HTTPException:
        raise HTTPException(404, f"repo desconocido: {rname}")
    branch = git(WT_DIR / dir, "branch", "--show-current").strip()
    if not branch:
        raise HTTPException(409, f"{dir}: el worktree no esta en ninguna rama (detached)")
    old = load_profile(prof)
    if old is not None and not old.get("worktree"):
        raise HTTPException(409, f"ya existe un perfil in-place llamado {prof}")
    if old is None:
        # sin lista de servicios = todos (los de otros repos corren desde el original)
        req = ProfileIn(name=prof, repos={rname: branch}, worktree=True)
    else:
        # se suma al perfil worktree existente (varios repos del mismo perfil)
        req = profile_in(prof, old)
        req.repos = {**req.repos, rname: branch}
    PROFILES_DIR.mkdir(exist_ok=True)
    profile_file(prof).write_text(profile_body(req, old.get("created_at") if old else None))
    start_job(prof, "prepare")
    return {"ok": True, "profile": prof,
            "msg": f"{rname} adoptado en el perfil {prof}; comprobando el worktree en segundo plano"}


def wt_remove_dirs(dirs: list[tuple[str, Path]], force: bool) -> list[str]:
    """git worktree remove de cada (repo, dir). Sin force, si alguno tiene cambios
    sin commitear no se borra nada (409). Las ramas y sus commits no se pierden."""
    dirty = [f"{d.name} ({n} fichero(s))" for _, d in dirs if (n := wt_dirty(d))]
    if dirty and not force:
        raise HTTPException(409, "cambios sin commitear en " + ", ".join(dirty)
                            + ": se perderian al borrar")
    errs = []
    for rname, d in dirs:
        try:
            git(repo_path(rname), "worktree", "remove", "--force", str(d), timeout=120)
            deps_stamp_file(d).unlink(missing_ok=True)
        except (RuntimeError, HTTPException) as e:
            errs.append(f"{rname}: {e.detail if isinstance(e, HTTPException) else e}")
    return errs


@app.post("/api/worktrees/orphans/{dir}/remove")
def api_wt_orphan_remove(dir: str, force: bool = False):
    d = WT_DIR / dir
    if "__" not in dir or not d.is_dir():
        raise HTTPException(404, f"worktree no existe: {dir}")
    _, _, rname = dir.partition("__")
    errs = wt_remove_dirs([(rname, d)], force)
    if errs:
        raise HTTPException(500, f"no se pudo eliminar: {errs[0]}")
    return {"ok": True}


@app.post("/api/services/wt/{profile}/{name}/up")
def api_wt_up(profile: str, name: str, force: bool = False):
    return api_up(f"{profile}/{name}", force=force)


@app.post("/api/services/wt/{profile}/{name}/down")
def api_wt_down_svc(profile: str, name: str):
    return api_down(f"{profile}/{name}")


@app.get("/api/logs/wt/{profile}/{name}")
def api_wt_logs(profile: str, name: str, lines: int = 150):
    return api_logs(f"{profile}/{name}", lines=lines)


def wt_down_profile(profile: str) -> list:
    with state_tx() as st:
        gone = [(sid, st["services"].pop(sid)) for sid in list(st["services"])
                if sid.startswith(f"{profile}/")]
        if gone and st.get("active_profile") == profile:
            st["active_profile"] = None
    out = []
    for sid, entry in gone:
        if entry:
            kill_process(entry["pid"])
        out.append((sid.split("/", 1)[1], {"ok": True, "msg": "down"}))
    return out


WS_DIR = ROOT / "workspaces"


@app.post("/api/worktrees/{name}/open")
def api_wt_open(name: str):
    w = wt_profiles().get(name)
    if not w:
        raise HTTPException(404, f"worktree no existe: {name}")
    dirs = [str(wt_dir_for(name, r)) for r in w["repos"]]
    if not dirs:
        raise HTTPException(404, f"sin directorios de worktree en disco para: {name}")
    code = shutil.which("code") or next((p for p in ("/opt/homebrew/bin/code", "/usr/local/bin/code") if Path(p).exists()), None)
    if not code:
        raise HTTPException(500, "comando 'code' no encontrado en PATH")
    ws_file = WS_DIR / f"{name}.code-workspace"
    ws_file.parent.mkdir(parents=True, exist_ok=True)
    ws_file.write_text(json.dumps({"folders": [{"path": d} for d in dirs]}, indent=2))
    subprocess.Popen([code, "--new-window", str(ws_file)], start_new_session=True)
    return {"ok": True, "opened": str(ws_file), "folders": dirs}


@app.post("/api/worktrees/{name}/down")
def api_wt_down(name: str):
    if name not in wt_profiles():
        raise HTTPException(404, f"worktree no existe: {name}")
    return {"ok": True, "services": wt_down_profile(name)}


@app.delete("/api/worktrees/{name}")
def api_wt_remove(name: str, force: bool = False):
    w = wt_profiles().get(name)
    if not w:
        raise HTTPException(404, f"worktree no existe: {name}")
    if load_state().get("jobs", {}).get(name, {}).get("status") == "running":
        raise HTTPException(409, f"{name}: esta trabajando; espera a que termine")
    dirs = [(r, wt_dir_for(name, r)) for r in w["repos"]]
    if any(wt_dirty(d) for _, d in dirs) and not force:
        wt_remove_dirs(dirs, force=False)  # lanza el 409 con el detalle
    wt_down_profile(name)
    errs = wt_remove_dirs(dirs, force=True)
    return {"ok": True, "errors": errs}


# --- MCP: devws como herramientas para un agente (Copilot, Claude...) ---------
# Mismo proceso y mismas funciones que la UI, en http://127.0.0.1:{web_port}/mcp.
# Las herramientas esperan a que acabe el trabajo en segundo plano y devuelven
# el resultado (con las rutas de los worktrees), asi el agente no tiene que sondear.

MCP_INSTRUCTIONS = """\
devws gestiona un workspace multi-repo: ramas, worktrees y servicios locales.
Un "espacio" es un perfil: in-place (los repos originales) o worktree (una copia
de trabajo por repo en worktrees/{espacio}__{repo}, con sus dependencias).

Cuando usarlo:
- Empieza llamando a devws_status para ver repos, ramas y lo que esta arrancado.
- Si la tarea toca 2 o mas repos, o es una feature/rama nueva que no debe pisar
  lo que hay en los repos originales, propon un espacio worktree (nombre corto,
  repos y rama de cada uno; base si la rama es nueva), pide confirmacion al
  usuario y llama a devws_create_workspace.
- Un cambio pequeno en un solo repo y en su rama actual se hace en el sitio, sin worktree.

Reglas:
- Para explorar o leer codigo no crees nada: lee los repos originales.
- Al crear un espacio no limites `services`: por defecto arranca todos (los de
  repos fuera del espacio, desde su carpeta original), que es lo que se espera.
- Tras crear un espacio, edita SOLO en las rutas que devuelve (dirs), nunca en
  los repos originales. Los repos que no estan en el espacio no se tocan.
- Crear un espacio solo prepara los worktrees (segundos): las dependencias se
  instalan al arrancarlo (devws_start). Si necesitas pasar tests o linters en un
  worktree sin arrancar servicios, llama antes a devws_install_deps. Solo se
  reinstala lo que cambio (uv.lock, package-lock.json...).
- Si a mitad de tarea hace falta otro repo, llama otra vez a
  devws_create_workspace con el mismo nombre y solo ese repo: se anade.
- Arrancar un espacio para otro que este corriendo (force) y borrar un espacio
  (devws_remove_workspace) solo con confirmacion explicita del usuario.
- Commits y push los hace el agente con git dentro de cada worktree, como siempre.

Anadir un repo o configurar su arranque (devws_add_repo, devws_configure_startup):
- En devws_status, un repo con startup=null no se puede arrancar: si la tarea lo
  necesita, ofrece configurarlo.
- Averigua el arranque leyendo el repo: .vscode/launch.json (la configuracion de
  depuracion suele tener modulo, app y argumentos), pyproject.toml / package.json
  (scripts start/dev), README, y PORT en .env. Copia el estilo de los servicios
  que ya hay en devws_status (p. ej. "./.venv/bin/python -m uvicorn ..." para
  python, "npm run start -- --port {port}" para npm).
- El comando es una lista de argumentos; {port} se sustituye por el puerto. Corre
  en la carpeta del repo (o de su worktree) con su .env. Usa un puerto que no
  tenga ya otro servicio.
- Dependencias (install): por defecto "uv sync" (python) o "npm install" (npm),
  que devws ejecuta en un worktree al arrancarlo o con devws_install_deps. Si el repo usa otra cosa (poetry,
  pnpm, yarn, pip install -r ..., un Makefile), pasala en install.
- Si el repo no tiene nada que arrancar y otros repos dependen de el (aparece en
  su pyproject.toml), es una libreria: library = esos repos.
- Propon al usuario el arranque (nombre, puerto, comando) y pide confirmacion
  antes de guardarlo: se escribe en config.toml.
"""

mcp = MCPServer("devws", instructions=MCP_INSTRUCTIONS)


def tool_call(fn, *args, **kwargs):
    """Errores de la API (HTTPException) como error de herramienta legible."""
    try:
        return fn(*args, **kwargs)
    except HTTPException as e:
        raise ToolError(str(e.detail))


def wait_job_done(profile: str, timeout: float = 1800) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = load_state().get("jobs", {}).get(profile) or {}
        if job.get("status") != "running":
            break
        time.sleep(1)
    else:
        raise ToolError(f"{profile}: sigue trabajando tras {timeout:.0f}s; mira devws_status")
    out = {"status": job.get("status"), "error": job.get("error")}
    if job.get("status") == "error":
        f = job_log(profile)
        out["log"] = f.read_text(errors="replace").splitlines()[-25:] if f.exists() else []
    return out


def space_info(name: str) -> dict:
    prof = load_profile(name) or {}
    state = load_state()
    is_wt = bool(prof.get("worktree"))
    svc_ids = [f"{name}/{s}" if is_wt else s for s in profile_services(prof)]
    conns = tcp_conns()
    known = {r["name"] for r in repos()}
    return {
        "name": name,
        "worktree": is_wt,
        "repos": prof.get("repos", {}),
        "dirs": {r: str(wt_dir_for(name, r) if is_wt else repo_path(r))
                 for r in prof.get("repos", {}) if r in known},
        "active": state.get("active_profile") == name,
        "services": [{k: st[k] for k in ("name", "port", "status")}
                     for st in (service_status(i, service(i.split("/")[-1]), state, conns) for i in svc_ids)],
    }


@mcp.tool()
def devws_status() -> dict:
    """Estado del workspace: repos (ruta, rama actual, cambios sin commitear),
    espacios (perfiles) con sus rutas, cual esta arrancado y trabajos en curso."""
    state = load_state()
    return {
        "workspace_root": str(WORKSPACE_ROOT),
        "repos": [{"name": r["name"], "path": str(repo_path(r["name"])), "kind": r.get("kind"),
                   "current": st.get("current"), "dirty": st.get("dirty"), "error": st.get("error"),
                   "startup": st.get("startup"), "install": st.get("install")}
                  for r, st in zip(repos(), api_repos())],
        "services": [{"name": s["name"], "repo": s["repo"], "port": s["port"], "cmd": s["cmd"]}
                     for s in services()],
        "libraries": CONFIG.get("libraries", []),
        "active_profile": state.get("active_profile"),
        "spaces": [space_info(n) for n in api_profiles_list()],
        "jobs": {p: j for p, j in state.get("jobs", {}).items() if j.get("status") == "running"},
    }


@mcp.tool()
def devws_create_workspace(name: str, repos: dict[str, str], bases: dict[str, str] | None = None,
                           services: list[str] | None = None, start: bool = False) -> dict:
    """Crea (o amplia, si ya existe) un espacio worktree: deja cada worktree en
    su rama, sin instalar dependencias (se instalan al arrancar o con
    devws_install_deps). Devuelve en `dirs` la ruta de cada worktree: edita ahi.

    name: nombre corto del espacio (letras, numeros, . _ -), p. ej. la feature.
    repos: repo -> rama. Si la rama no existe se crea desde bases[repo] o desde
      la rama por defecto de origin.
    services: NO lo pases salvo que el usuario pida arrancar solo algunos: sin
      el, el espacio arranca todos los servicios (los de repos que no estan en el
      espacio corren desde su carpeta original).
    start: arrancar tambien los servicios (para el espacio que este corriendo).
    """
    old = load_profile(name)
    if old is not None and not old.get("worktree"):
        raise ToolError(f"{name} es un perfil in-place; usa otro nombre")
    for r in repos:
        tool_call(repo_path, r)
    if old is None:
        req = ProfileIn(name=name, repos=repos, worktree=True, services=services or [], bases=bases or {})
        tool_call(api_profiles_save, req)
    else:
        req = profile_in(name, old)
        req.repos = {**req.repos, **repos}
        req.bases = {**req.bases, **(bases or {})}
        if services is not None:
            req.services = services
        tool_call(api_profiles_update, req)
    if start:
        tool_call(api_profile_start, name, force=True)
    else:
        tool_call(start_job, name, "prepare")
    return {**wait_job_done(name), **space_info(name)}


@mcp.tool()
def devws_install_deps(name: str) -> dict:
    """Instala las dependencias de los worktrees del espacio (solo las que
    cambiaron) sin arrancar servicios: para pasar tests o linters en ellos."""
    tool_call(api_profile_install, name)
    return {**wait_job_done(name), **space_info(name)}


@mcp.tool()
def devws_start(name: str, force: bool = False) -> dict:
    """Arranca un espacio (prepara/checkout + servicios). Si hay otro arrancado
    falla salvo force=true, que lo para antes: pide confirmacion al usuario."""
    tool_call(api_profile_start, name, force=force)
    return {**wait_job_done(name), **space_info(name)}


@mcp.tool()
def devws_stop(name: str) -> dict:
    """Para los servicios de un espacio."""
    tool_call(api_profile_stop, name)
    return {**wait_job_done(name), **space_info(name)}


@mcp.tool()
def devws_remove_workspace(name: str, force: bool = False) -> dict:
    """Borra los worktrees de un espacio y su perfil (las ramas y commits se
    conservan). Con cambios sin commitear falla salvo force=true, que los pierde:
    solo con confirmacion explicita del usuario."""
    prof = tool_call(_profile_or_404, name)
    if not prof.get("worktree"):
        raise ToolError(f"{name} es un perfil in-place: no tiene worktrees que borrar")
    errors = []
    if name in wt_profiles():
        errors = tool_call(api_wt_remove, name, force=force)["errors"]
    api_profiles_delete(name)
    return {"ok": not errors, "errors": errors}


@mcp.tool()
def devws_add_repo(path: str, name: str | None = None, kind: str | None = None,
                   service: ServiceIn | None = None, library: list[str] | None = None,
                   install: list[str] | None = None) -> dict:
    """Registra un repo git en devws, con su arranque si se indica.

    path: ruta absoluta del repo (admite ~).
    name: por defecto, el nombre de la carpeta. kind: "python" o "npm" (por
      defecto npm si hay package.json, si no python): decide como se instalan
      las dependencias en los worktrees.
    service: como arrancarlo, {name, port, cmd: [args con {port}]}.
    library: si es una libreria, los repos que la usan (en vez de service).
    install: instalador de dependencias en los worktrees (por defecto uv sync /
      npm install segun kind; [] = no instalar nada).
    Sin service ni library se registra pendiente de configurar el arranque.
    """
    d = Path(path).expanduser()
    if kind is None:
        kind = "npm" if (d / "package.json").exists() else "python"
    body = RepoIn(name=name or d.name, path=path, kind=kind, service=service, library=library,
                  install=install)
    tool_call(api_repos_add, body)
    return next(r for r in devws_status()["repos"] if r["name"] == body.name)


@mcp.tool()
def devws_configure_startup(repo: str, service: ServiceIn | None = None,
                            library: list[str] | None = None, install: list[str] | None = None) -> dict:
    """Configura el arranque de un repo registrado que aun no lo tiene
    (startup=null en devws_status): un servicio {name, port, cmd: [args con
    {port}]} o, si es una libreria, los repos que la usan. install: instalador
    de dependencias si no es el de su kind ([] = no instalar nada)."""
    tool_call(api_repos_startup, repo, StartupIn(service=service, library=library, install=install))
    return next(r for r in devws_status()["repos"] if r["name"] == repo)


@mcp.tool()
def devws_repo_config(repo: str) -> dict:
    """Configuracion completa de un repo: carpeta, tipo, instalador de
    dependencias, ficheros que se copian al worktree, si tiene .env, y su
    arranque (servicios con puerto y comando, o libreria). Usala cuando un
    servicio no arranca bien, junto con los logs."""
    return tool_call(repo_config, repo)


@mcp.tool()
def devws_update_repo(repo: str, path: str | None = None, kind: str | None = None,
                      install: list[str] | None = None, copy_files: list[str] | None = None,
                      services: list[ServiceIn] | None = None, library: list[str] | None = None) -> dict:
    """Cambia la configuracion de un repo; solo los campos que se pasan.
    services: sus servicios por nombre (no se renombran) con port y cmd nuevos.
    Pide confirmacion al usuario antes: se escribe en config.toml."""
    body = RepoUpdate(path=path, kind=kind, install=install, copy_files=copy_files,
                      services=services, library=library)
    return tool_call(api_repos_update, repo, body)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


reset_interrupted_jobs()
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
# al final: atiende /mcp y lo que no haya resuelto ninguna ruta de arriba
app.mount("/", mcp.streamable_http_app(stateless_http=True, json_response=True))


if __name__ == "__main__":
    import uvicorn
    print(f"devws en http://127.0.0.1:{WEB_PORT}")
    uvicorn.run(app, host="127.0.0.1", port=WEB_PORT, log_level="warning")
