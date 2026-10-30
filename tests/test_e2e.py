"""Pruebas end-to-end de devws contra un workspace de mentira.

Crea en un directorio temporal repos git reales (con remoto "origin"), una copia
de .devws con su propio config.toml y arranca server.py en un puerto libre.
Los servicios de prueba devuelven su cwd, asi se comprueba desde donde corren.
Ejecutar: uv run pytest -v
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

DEVWS = Path(__file__).resolve().parent.parent

PY_APP = '''import os

async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    body = os.getcwd().encode()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": body})
'''

NODE_APP = '''const http = require("http");
const port = Number(process.argv[process.argv.indexOf("--port") + 1]);
http.createServer((req, res) => res.end(process.cwd())).listen(port, "127.0.0.1");
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sh(cwd: Path, *args: str) -> str:
    res = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    assert res.returncode == 0, f"{args}: {res.stderr}"
    return res.stdout


def make_repo(root: Path, rel: str, files: dict[str, str], branches: list[str]) -> None:
    """Repo en root/rel con remoto bare en root/_remotes; rama main + `branches` en origin."""
    remote = root / "_remotes" / (rel.replace("/", "_") + ".git")
    remote.parent.mkdir(parents=True, exist_ok=True)
    sh(root, "git", "init", "-q", "--bare", "-b", "main", str(remote))
    d = root / rel
    d.mkdir(parents=True)
    sh(d, "git", "init", "-q", "-b", "main")
    sh(d, "git", "config", "user.email", "test@devws")
    sh(d, "git", "config", "user.name", "test")
    sh(d, "git", "config", "core.autocrlf", "false")
    for name, content in {**files, ".gitignore": ".env\n.venv/\nnode_modules/\n__pycache__/\nuv.lock\n"}.items():
        (d / name).write_text(content)
    sh(d, "git", "add", ".")
    sh(d, "git", "commit", "-q", "-m", "init")
    sh(d, "git", "remote", "add", "origin", str(remote))
    sh(d, "git", "push", "-q", "origin", "main")
    for b in branches:
        sh(d, "git", "checkout", "-q", "-b", b)
        (d / "BRANCH").write_text(b)
        sh(d, "git", "add", ".")
        sh(d, "git", "commit", "-q", "-m", b)
        sh(d, "git", "push", "-q", "origin", b)
        sh(d, "git", "checkout", "-q", "main")
        sh(d, "git", "branch", "-q", "-D", b)
    (d / ".env").write_text("DEVWS_TEST=1\n")


def py_pyproject(name: str) -> str:
    return f'[project]\nname = "{name}"\nversion = "0"\nrequires-python = ">=3.12"\ndependencies = []\n\n[tool.uv]\npackage = false\n'


class Client:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"

    def call(self, method: str, path: str, body=None, ok=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-Devws": "1"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        out = json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode()
        if ok:
            assert status < 400, f"{method} {path} -> {status}: {out}"
        return (status, out) if not ok else out

    get = lambda self, p, **kw: self.call("GET", p, **kw)
    post = lambda self, p, body=None, **kw: self.call("POST", p, body, **kw)
    put = lambda self, p, body=None, **kw: self.call("PUT", p, body, **kw)
    delete = lambda self, p, **kw: self.call("DELETE", p, **kw)


def wait_for(cond, timeout: float = 60, msg: str = "condicion"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = cond()
        if v:
            return v
        time.sleep(0.3)
    raise AssertionError(f"timeout esperando {msg}")


def fetch(port: int) -> str | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            return r.read().decode()
    except Exception:
        return None


@pytest.fixture(scope="module")
def ws(tmp_path_factory):
    root = tmp_path_factory.mktemp("ws")
    make_repo(root, "api", {"app.py": PY_APP, "pyproject.toml": py_pyproject("api")}, ["feat-a"])
    make_repo(root, "commons/worker", {"app.py": PY_APP, "pyproject.toml": py_pyproject("worker")}, ["feat-a"])
    make_repo(root, "web", {"server.js": NODE_APP,
                            "package.json": json.dumps({"name": "web", "version": "0.0.0",
                                                        "scripts": {"start": "node server.js"}})}, ["feat-a"])
    make_repo(root, "lib", {"pyproject.toml": py_pyproject("lib")}, ["feat-a"])

    dws = root / ".devws"
    dws.mkdir()
    shutil.copy(DEVWS / "server.py", dws / "server.py")
    shutil.copytree(DEVWS / "static", dws / "static")
    ports = {n: free_port() for n in ("web_ui", "api", "worker", "web")}
    py = json.dumps(sys.executable)
    npm = json.dumps(shutil.which("npm") or "npm")
    (dws / "config.toml").write_text(f"""[meta]
web_port = {ports["web_ui"]}

[[repos]]
name = "api"
path = "api"
kind = "python"
copy = [".env"]

[[repos]]
name = "worker"
path = "commons/worker"
kind = "python"

[[repos]]
name = "web"
path = "web"
kind = "npm"

[[repos]]
name = "lib"
path = "lib"
kind = "python"

[[services]]
name = "worker"
repo = "worker"
port = {ports["worker"]}
cmd = [{py}, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "{{port}}"]

[[services]]
name = "api"
repo = "api"
port = {ports["api"]}
cmd = [{py}, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "{{port}}"]

[[services]]
name = "web"
repo = "web"
port = {ports["web"]}
cmd = [{npm}, "run", "start", "--", "--port", "{{port}}"]

[[libraries]]
name = "lib"
consumers = ["api"]
""")
    env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    log = open(root / "server.log", "ab")
    c = Client(ports["web_ui"])
    server = {}

    def start():
        server["proc"] = subprocess.Popen([sys.executable, str(dws / "server.py")], cwd=dws,
                                          stdout=log, stderr=subprocess.STDOUT, env=env)
        wait_for(lambda: fetch(ports["web_ui"]) is not None, 30, "server devws")

    def stop():
        server["proc"].terminate()
        server["proc"].wait(10)
        wait_for(lambda: fetch(ports["web_ui"]) is None, 10, "server devws parado")

    start()
    yield {"root": root, "dws": dws, "ports": ports, "c": c,
           "restart": lambda: (stop(), start())}
    for s in c.get("/api/state")["services"]:
        if s["status"] in ("up", "starting"):
            c.post(f"/api/services/{urllib.parse.quote(s['id'], safe='')}/down", ok=False)
    stop()
    log.close()


def svc(c: Client, sid: str) -> dict:
    return next(s for s in c.get("/api/state")["services"] if s["id"] == sid)


def test_state_y_repos(ws):
    c = ws["c"]
    st = c.get("/api/state")
    assert [s["id"] for s in st["services"]] == ["worker", "api", "web"]
    assert all(s["status"] == "down" for s in st["services"])
    repos = {r["name"]: r for r in c.get("/api/repos")}
    assert repos["api"]["current"] == "main"
    assert "feat-a" in repos["api"]["branches"]


def test_ui_carga(ws):
    assert "<title>devws" in fetch(ws["ports"]["web_ui"])


def test_up_down_in_place(ws):
    c, ports = ws["c"], ws["ports"]
    r = c.post("/api/services/api/up")
    assert r["ok"] and "up en" in r["msg"], r
    assert fetch(ports["api"]) == str(ws["root"] / "api")
    assert svc(c, "api")["status"] == "up"
    c.post("/api/services/api/down")
    wait_for(lambda: fetch(ports["api"]) is None, 15, "api caido")
    assert svc(c, "api")["status"] == "down"


def test_checkout_in_place(ws):
    c = ws["c"]
    r = c.post("/api/checkout", {"items": [{"repo": "api", "branch": "feat-a"}]})
    assert r == [{"repo": "api", "ok": True, "branch": "feat-a"}]
    assert (ws["root"] / "api" / "BRANCH").read_text() == "feat-a"
    c.post("/api/checkout", {"items": [{"repo": "api", "branch": "main"}]})


def test_perfil_crud(ws):
    c = ws["c"]
    body = {"name": "p1", "repos": {"api": "main"}, "services": ["api"], "worktree": False}
    c.post("/api/profiles", body)
    assert "p1" in c.get("/api/profiles")
    assert c.post("/api/profiles", body, ok=False)[0] == 409
    c.put("/api/profiles", {**body, "services": []})
    assert c.get("/api/profiles/p1")["services"]["services"] == []
    c.delete("/api/profiles/p1")
    assert "p1" not in c.get("/api/profiles")


def wt_info(c: Client, prof: str) -> dict | None:
    return c.get("/api/state")["worktrees"].get(prof)


def wait_job(c: Client, prof: str, expect: str = "ok") -> dict:
    """Espera a que acabe el trabajo del perfil; devuelve job + info de worktrees."""
    j = wait_for(lambda: (j := c.get("/api/state")["jobs"].get(prof)) and j["status"] != "running" and j,
                 180, f"trabajo de {prof}")
    assert j["status"] == expect, (j, c.get(f"/api/logs/{prof}--actividad")["lines"][-20:])
    return {**(wt_info(c, prof) or {}), **j}


def prepare(c: Client, prof: str, expect: str = "ok") -> dict:
    """Prepara los worktrees del perfil (sin arrancar servicios) y espera."""
    c.post(f"/api/profiles/{prof}/prepare")
    return wait_job(c, prof, expect)


def install(c: Client, prof: str, expect: str = "ok") -> dict:
    """Worktrees + dependencias (sin arrancar servicios) y espera."""
    c.post(f"/api/profiles/{prof}/install")
    return wait_job(c, prof, expect)


def installed_now(c: Client, prof: str) -> bool:
    """El ultimo trabajo del perfil ejecuto algun instalador de dependencias."""
    return any("instalando dependencias" in l for l in c.get(f"/api/logs/{prof}--actividad")["lines"])


def test_worktree_ciclo(ws):
    c, ports, dws = ws["c"], ws["ports"], ws["dws"]
    c.post("/api/profiles", {"name": "wt1", "repos": {"api": "feat-a", "worker": "feat-a"},
                             "services": ["worker", "api"], "worktree": True})
    w = prepare(c, "wt1")
    assert w["repos"] == {"api": "feat-a", "worker": "feat-a"} and w["pending"] == []
    wt_api = dws / "worktrees" / "wt1__api"
    assert (wt_api / "BRANCH").read_text() == "feat-a"
    assert not (wt_api / ".venv").exists(), "preparar no instala dependencias"
    r = c.post("/api/services/wt/wt1/api/up?force=true")
    assert not r["ok"] and r["error"] == "deps", r
    install(c, "wt1")
    assert (wt_api / ".venv").is_dir(), "uv sync no se ejecuto en el worktree"
    assert (wt_api / ".env").read_text() == "DEVWS_TEST=1\n", 'copy = [".env"] copia el ignorado'
    assert not (dws / "worktrees" / "wt1__worker" / ".env").exists(), "sin copy no se copia nada"
    ids = [s["id"] for s in c.get("/api/state")["services"] if s.get("profile") == "wt1"]
    assert ids == ["wt1/worker", "wt1/api"], "solo los servicios del perfil, en su orden"
    r = c.post("/api/services/wt/wt1/api/up?force=true")
    assert r["ok"] and "up en" in r.get("msg", ""), r
    assert fetch(ports["api"]) == str(wt_api)
    s = svc(c, "wt1/api")
    assert s["status"] == "up" and s["wt"] and s["repo_branch"] == "feat-a"
    c.post("/api/services/wt/wt1/api/down")
    wait_for(lambda: fetch(ports["api"]) is None, 15, "api wt caido")
    c.delete("/api/worktrees/wt1")
    assert not wt_api.exists()
    c.delete("/api/profiles/wt1")


def test_rechaza_otro_origen(ws):
    req = urllib.request.Request(ws["c"].base + "/api/services/api/up", method="POST",
                                 headers={"Origin": "https://evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 403
    assert svc(ws["c"], "api")["status"] == "down"


@pytest.mark.parametrize("name", ["../config", "a__b", "con espacio", 'x"y'])
def test_nombre_de_perfil_invalido(ws, name):
    body = {"name": name, "repos": {}, "services": [], "worktree": False}
    assert ws["c"].post("/api/profiles", body, ok=False)[0] == 400
    assert ws["c"].put("/api/profiles", body, ok=False)[0] == 400
    assert (ws["dws"] / "config.toml").exists()


def test_perfil_in_place_arranca_en_orden_del_perfil(ws):
    c = ws["c"]
    c.post("/api/profiles", {"name": "orden", "repos": {"api": "main", "worker": "main"},
                             "services": ["api", "worker"], "worktree": False})
    c.post("/api/profiles/orden/start")
    wait_job(c, "orden")
    log = " | ".join(c.get("/api/logs/orden--actividad")["lines"])
    assert log.index("arrancando api (1/2)") < log.index("arrancando worker (2/2)"), log
    assert c.get("/api/state")["active_profile"] == "orden"
    assert svc(c, "api")["status"] == "up" and svc(c, "worker")["status"] == "up"
    c.post("/api/profiles/orden/stop")
    wait_job(c, "orden")
    st = c.get("/api/state")
    assert st["active_profile"] is None and svc(c, "api")["status"] == "down"
    c.delete("/api/profiles/orden")


def git_out(d: Path, *args: str) -> str:
    return sh(d, "git", *args).strip()


def test_perfil_parcial_servicios_desde_el_original(ws):
    c, ports, root, dws = ws["c"], ws["ports"], ws["root"], ws["dws"]
    c.post("/api/profiles", {"name": "parcial", "repos": {"api": "feat-a"},
                             "services": ["web", "api"], "worktree": True})
    w = install(c, "parcial")
    assert list(w["repos"]) == ["api"]
    assert not (dws / "worktrees" / "parcial__web").exists(), "web no esta en el perfil: sin worktree"
    assert c.post("/api/services/wt/parcial/web/up?force=true")["ok"]
    assert fetch(ports["web"]) == str(root / "web")
    assert svc(c, "parcial/web")["from_original"] is True
    assert c.post("/api/services/wt/parcial/api/up?force=true")["ok"]
    assert fetch(ports["api"]) == str(dws / "worktrees" / "parcial__api")
    c.post("/api/worktrees/parcial/down")
    wait_for(lambda: fetch(ports["web"]) is None and fetch(ports["api"]) is None, 15, "servicios caidos")
    c.delete("/api/worktrees/parcial")
    c.delete("/api/profiles/parcial")


def test_rama_nueva_desde_base_sin_tracking(ws):
    c, dws = ws["c"], ws["dws"]
    c.post("/api/profiles", {"name": "nueva", "repos": {"api": "feat-nueva"}, "worktree": True,
                             "bases": {"api": "feat-a"}, "services": ["api"]})
    assert c.get("/api/profiles/nueva")["bases"] == {"api": "feat-a"}
    prepare(c, "nueva")
    d = dws / "worktrees" / "nueva__api"
    assert git_out(d, "branch", "--show-current") == "feat-nueva"
    assert (d / "BRANCH").read_text() == "feat-a", "debe partir de la base, no del HEAD del original"
    res = subprocess.run(["git", "rev-parse", "--abbrev-ref", "@{u}"], cwd=d, capture_output=True)
    assert res.returncode != 0, "una rama nueva no debe seguir a la rama base"
    c.delete("/api/worktrees/nueva")
    c.delete("/api/profiles/nueva")


def test_rama_nueva_sin_base_parte_de_la_rama_por_defecto_de_origin(ws):
    c, dws = ws["c"], ws["dws"]
    worker = ws["root"] / "commons" / "worker"
    sh(worker, "git", "checkout", "-q", "feat-a")  # el original en otra rama: no debe influir
    try:
        c.post("/api/profiles", {"name": "sinbase", "repos": {"worker": "feat-x"}, "worktree": True})
        prepare(c, "sinbase")
        assert not (dws / "worktrees" / "sinbase__worker" / "BRANCH").exists(), "debe partir de origin/main"
        c.delete("/api/worktrees/sinbase")
        c.delete("/api/profiles/sinbase")
        assert "sinbase" not in c.get("/api/state")["worktrees"]
    finally:
        sh(worker, "git", "checkout", "-q", "main")


def test_rama_abierta_en_otro_sitio_no_se_fuerza(ws):
    c, dws = ws["c"], ws["dws"]
    c.post("/api/profiles", {"name": "dup", "repos": {"api": "main"}, "worktree": True})
    w = prepare(c, "dup", expect="error")
    assert "ya esta abierta" in w["error"]
    assert not (dws / "worktrees" / "dup__api").exists()
    c.delete("/api/profiles/dup")


def test_editar_perfil_y_sincronizar(ws):
    c, dws = ws["c"], ws["dws"]
    body = {"name": "sync", "repos": {"api": "feat-a"}, "services": ["api"], "worktree": True}
    c.post("/api/profiles", body)
    prepare(c, "sync")
    c.put("/api/profiles", {**body, "repos": {"api": "feat-s", "worker": "feat-a"}, "bases": {"api": "main"}})
    assert sorted(wt_info(c, "sync")["pending"]) == ["api", "worker"]
    w = prepare(c, "sync")
    assert w["pending"] == [] and w["repos"] == {"api": "feat-s", "worker": "feat-a"}
    assert git_out(dws / "worktrees" / "sync__api", "branch", "--show-current") == "feat-s"
    c.delete("/api/worktrees/sync")
    c.delete("/api/profiles/sync")


def test_borrar_worktree_con_cambios_pide_confirmacion(ws):
    c, dws = ws["c"], ws["dws"]
    c.post("/api/profiles", {"name": "sucio", "repos": {"worker": "feat-a"}, "worktree": True})
    prepare(c, "sucio")
    d = dws / "worktrees" / "sucio__worker"
    (d / "nuevo.txt").write_text("trabajo sin commitear")
    status, out = c.delete("/api/worktrees/sucio", ok=False)
    assert status == 409 and "sin commitear" in out["detail"]
    assert d.exists()
    c.delete("/api/worktrees/sucio?force=true")
    assert not d.exists()
    assert "feat-a" in git_out(ws["root"] / "commons" / "worker", "branch", "--list", "feat-a"), \
        "la rama sigue existiendo en el repo original"
    c.delete("/api/profiles/sucio")


def test_adoptar_huerfanos_en_un_perfil_existente(ws):
    c, dws, root = ws["c"], ws["dws"], ws["root"]
    c.post("/api/profiles", {"name": "adop", "repos": {"api": "feat-a"}, "worktree": True})
    prepare(c, "adop")
    sh(root / "commons" / "worker", "git", "worktree", "add", "-q", "-b", "feat-w",
       str(dws / "worktrees" / "adop__worker"))
    orphans = c.get("/api/worktrees/orphans")
    assert [o["dir"] for o in orphans] == ["adop__worker"]
    r = c.post("/api/worktrees/orphans/adop__worker/adopt")
    assert r["profile"] == "adop"
    assert c.get("/api/profiles/adop")["repos"] == {"api": "feat-a", "worker": "feat-w"}
    w = wait_job(c, "adop")
    assert set(w["repos"]) == {"api", "worker"}
    assert c.get("/api/worktrees/orphans") == []
    c.delete("/api/worktrees/adop")
    c.delete("/api/profiles/adop")


def test_preparacion_interrumpida_por_reinicio_permite_reintentar(ws):
    c, dws = ws["c"], ws["dws"]
    c.post("/api/profiles", {"name": "reini", "repos": {"worker": "feat-a"}, "worktree": True})
    state_file = dws / "state.json"
    st = json.loads(state_file.read_text())
    st.setdefault("jobs", {})["reini"] = {"action": "start", "status": "running", "step": "uv sync"}
    state_file.write_text(json.dumps(st))
    ws["restart"]()
    j = c.get("/api/state")["jobs"]["reini"]
    assert j["status"] == "error" and "interrumpido" in j["error"]
    prepare(c, "reini")
    c.delete("/api/worktrees/reini")
    c.delete("/api/profiles/reini")


def test_arrancar_parar_y_cambiar_de_espacio(ws):
    c, ports, dws, root = ws["c"], ws["ports"], ws["dws"], ws["root"]
    c.post("/api/profiles", {"name": "ea", "repos": {"api": "feat-a"}, "services": ["worker", "api"], "worktree": True})
    c.post("/api/profiles", {"name": "eb", "repos": {"worker": "main"}, "services": ["worker"], "worktree": False})
    # arrancar un perfil worktree sin preparar: crea worktrees, deps y servicios
    c.post("/api/profiles/ea/start")
    wait_job(c, "ea")
    st = c.get("/api/state")
    assert st["active_profile"] == "ea"
    assert fetch(ports["api"]) == str(dws / "worktrees" / "ea__api")
    assert fetch(ports["worker"]) == str(root / "commons" / "worker"), "worker no esta en el perfil: original"
    # otro espacio arrancado: 409 sin force, con force cambia de espacio
    status, out = c.post("/api/profiles/eb/start", ok=False)
    assert status == 409 and "esta arrancado ea" in out["detail"]
    c.post("/api/profiles/eb/start?force=true")
    wait_job(c, "eb")
    st = c.get("/api/state")
    assert st["active_profile"] == "eb"
    assert fetch(ports["api"]) is None, "el espacio anterior se paro"
    assert svc(c, "worker")["status"] == "up"
    # un trabajo a la vez
    c.post("/api/profiles/eb/stop")
    status, out = c.post("/api/profiles/ea/start?force=true", ok=False)
    assert status == 409 and "espera" in out["detail"], out
    wait_job(c, "eb")
    assert c.get("/api/state")["active_profile"] is None
    assert all(s["status"] == "down" for s in c.get("/api/state")["services"])
    c.delete("/api/worktrees/ea")
    for p in ("ea", "eb"):
        c.delete(f"/api/profiles/{p}")


def test_error_al_arrancar_se_ve_en_el_trabajo(ws):
    c = ws["c"]
    c.post("/api/profiles", {"name": "roto", "repos": {"api": "main"}, "worktree": True})
    j = (c.post("/api/profiles/roto/start"), wait_job(c, "roto", expect="error"))[1]
    assert "ya esta abierta" in j["error"]
    assert c.get("/api/state")["active_profile"] is None
    assert all(s["status"] == "down" for s in c.get("/api/state")["services"]), "no arranca servicios si falla"
    c.delete("/api/profiles/roto")


def mcp_rpc(c: Client, method: str, params: dict | None = None) -> dict:
    """JSON-RPC contra /mcp como lo haria un cliente MCP (Copilot): sin Origin."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(c.base + "/mcp", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.loads(r.read())
    assert "error" not in out, out
    return out["result"]


def mcp_tool(c: Client, tool: str, **args) -> tuple[bool, dict | str]:
    r = mcp_rpc(c, "tools/call", {"name": tool, "arguments": args})
    text = r["content"][0]["text"]
    return (True, text) if r.get("isError") else (False, json.loads(text))


def test_mcp_crea_amplia_y_borra_un_espacio(ws):
    c, dws = ws["c"], ws["dws"]
    init = mcp_rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "0"}})
    assert "worktree" in init["instructions"], "las instrucciones de uso viajan con el servidor"
    tools = {t["name"] for t in mcp_rpc(c, "tools/list")["tools"]}
    assert {"devws_status", "devws_create_workspace", "devws_start", "devws_stop",
            "devws_remove_workspace"} <= tools

    err, st = mcp_tool(c, "devws_status")
    assert not err, st
    assert {r["name"] for r in st["repos"]} == {"api", "worker", "web", "lib"}

    err, w = mcp_tool(c, "devws_create_workspace", name="mcp1", repos={"api": "feat-a"}, services=["api"])
    assert not err and w["status"] == "ok", w
    assert w["dirs"] == {"api": str(dws / "worktrees" / "mcp1__api")}
    assert (dws / "worktrees" / "mcp1__api" / "BRANCH").read_text() == "feat-a"

    err, w = mcp_tool(c, "devws_create_workspace", name="mcp1", repos={"worker": "mcp-nueva"},
                      bases={"worker": "feat-a"})
    assert not err and w["status"] == "ok", w
    assert w["repos"] == {"api": "feat-a", "worker": "mcp-nueva"}, "se anade al espacio existente"
    assert (dws / "worktrees" / "mcp1__worker" / "BRANCH").read_text() == "feat-a"

    err, msg = mcp_tool(c, "devws_create_workspace", name="mcp1", repos={"nope": "main"})
    assert err and "repo desconocido" in msg

    err, w = mcp_tool(c, "devws_start", name="mcp1")
    assert not err and w["status"] == "ok" and w["active"], w
    assert w["services"] == [{"name": "api", "port": ws["ports"]["api"], "status": "up"}]
    assert fetch(ws["ports"]["api"]) == w["dirs"]["api"], "corre desde el worktree"
    err, w = mcp_tool(c, "devws_stop", name="mcp1")
    assert not err and not w["active"] and w["services"][0]["status"] == "down", w

    (dws / "worktrees" / "mcp1__api" / "sucio.txt").write_text("x")
    err, msg = mcp_tool(c, "devws_remove_workspace", name="mcp1")
    assert err and "sin commitear" in msg
    err, r = mcp_tool(c, "devws_remove_workspace", name="mcp1", force=True)
    assert not err and r["ok"], r
    assert not (dws / "worktrees" / "mcp1__api").exists()
    assert "mcp1" not in c.get("/api/profiles")


def test_anadir_repo_con_path_absoluto(ws):
    c, root = ws["c"], ws["root"]
    st, out = c.post("/api/repos", {"name": "rel", "path": "api"}, ok=False)
    assert st == 400 and "absoluto" in out["detail"]
    st, out = c.post("/api/repos", {"name": "nada", "path": str(root / "no-existe")}, ok=False)
    assert st == 400 and "no hay repo git" in out["detail"]
    c.post("/api/repos", {"name": "api2", "path": str(root / "api")})
    assert f'path = "{(root / "api").resolve().as_posix()}"' in (ws["dws"] / "config.toml").read_text()
    assert {r["name"]: r for r in c.get("/api/repos")}["api2"]["current"] == "main"
    c.delete("/api/repos/api2")


def test_anadir_repo_con_su_arranque_y_quitarlo(ws):
    c, root, dws, ports = ws["c"], ws["root"], ws["dws"], ws["ports"]
    cfg = dws / "config.toml"
    before = cfg.read_text()
    port = free_port()
    py = sys.executable
    svc_body = {"name": "api2", "port": port,
                "cmd": [py, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "{port}"]}

    st, out = c.post("/api/repos", {"name": "x", "path": str(root / "api"),
                                    "service": {**svc_body, "name": "x", "port": ports["api"]}}, ok=False)
    assert st == 409 and "ya lo usa el servicio api" in out["detail"]
    assert cfg.read_text() == before, "si el arranque no vale no se anade nada"
    st, out = c.post("/api/repos", {"name": "x", "path": str(root / "api"),
                                    "service": svc_body, "library": []}, ok=False)
    assert st == 400

    r = c.post("/api/repos", {"name": "api2", "path": str(root / "api"), "service": svc_body})
    assert r["startup"] == "service"
    assert [s["id"] for s in c.get("/api/state")["services"]][-1] == "api2", "al final del orden de arranque"
    assert {r["name"]: r for r in c.get("/api/repos")}["api2"]["startup"] == "service"
    r = c.post("/api/services/api2/up")
    assert r["ok"], r
    assert fetch(port) == str(root / "api")
    st, out = c.delete("/api/repos/api2", ok=False)
    assert st == 409 and "arrancados" in out["detail"]
    c.post("/api/services/api2/down")
    wait_for(lambda: fetch(port) is None, 15, "api2 caido")

    r = c.post("/api/repos", {"name": "lib2", "path": str(root / "lib")})
    assert r["startup"] is None, "sin arranque se anade igual, pendiente de configurar"
    r = c.post("/api/repos/lib2/startup", {"library": ["api", "api2"]})
    assert r["startup"] == "library"
    assert c.post("/api/repos/lib2/startup", {"library": []}, ok=False)[0] == 409
    st, out = c.delete("/api/repos/api2", ok=False)
    assert st == 409 and "lib2" in out["detail"], "no se quita un consumidor de una libreria"

    c.delete("/api/repos/lib2")
    c.delete("/api/repos/api2")
    assert tomllib.loads(cfg.read_text()) == tomllib.loads(before), "quitar deja la config como estaba"
    assert "api2" not in [s["id"] for s in c.get("/api/state")["services"]]


def test_mcp_anade_repo_y_configura_su_arranque(ws):
    c, root, dws = ws["c"], ws["root"], ws["dws"]
    before = (dws / "config.toml").read_text()
    assert "launch.json" in mcp_rpc(c, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"}})["instructions"]

    err, r = mcp_tool(c, "devws_add_repo", path=str(root / "web"), name="web2")
    assert not err and r["kind"] == "npm" and r["startup"] is None, r
    err, msg = mcp_tool(c, "devws_configure_startup", repo="web2",
                        service={"name": "web2", "port": ws["ports"]["web"], "cmd": ["npm", "start"]})
    assert err and "ya lo usa el servicio web" in msg
    port = free_port()
    err, r = mcp_tool(c, "devws_configure_startup", repo="web2",
                      service={"name": "web2", "port": port, "cmd": ["npm", "run", "start", "--", "--port", "{port}"]})
    assert not err and r["startup"] == "service", r
    err, st = mcp_tool(c, "devws_status")
    assert {"name": "web2", "repo": "web2", "port": port,
            "cmd": ["npm", "run", "start", "--", "--port", "{port}"]} in st["services"]

    err, r = mcp_tool(c, "devws_add_repo", path=str(root / "lib"), name="lib3", library=["api"])
    assert not err and r["startup"] == "library", r
    assert {"name": "lib3", "consumers": ["api"]} in mcp_tool(c, "devws_status")[1]["libraries"]

    c.delete("/api/repos/lib3")
    c.delete("/api/repos/web2")
    assert tomllib.loads((dws / "config.toml").read_text()) == tomllib.loads(before)


def test_instalador_de_dependencias_propio(ws):
    c, root, dws = ws["c"], ws["root"], ws["dws"]
    cfg = dws / "config.toml"
    before = cfg.read_text()
    c.post("/api/repos", {"name": "lib5", "path": str(root / "lib"), "install": "uv sync", "library": []})
    assert "install" not in tomllib.loads(cfg.read_text())["repos"][-1], "el de su kind no se escribe"
    c.delete("/api/repos/lib5")

    inst = [sys.executable, "-c", "open('INSTALADO', 'w').write('ok')"]
    r = c.post("/api/repos", {"name": "lib4", "path": str(root / "lib"), "install": inst, "library": []})
    assert {r["name"]: r for r in c.get("/api/repos")}["lib4"]["install"] == inst
    c.post("/api/profiles", {"name": "inst", "repos": {"lib4": "inst-test"}, "bases": {"lib4": "main"},
                             "services": [], "worktree": True})
    install(c, "inst")
    assert (dws / "worktrees" / "inst__lib4" / "INSTALADO").read_text() == "ok"
    assert not (dws / "worktrees" / "inst__lib4" / ".venv").exists(), "no se lanza uv sync"
    c.delete("/api/worktrees/inst?force=true")
    c.delete("/api/profiles/inst")
    c.delete("/api/repos/lib4")
    assert tomllib.loads(cfg.read_text()) == tomllib.loads(before)


def test_dependencias_solo_si_cambian_sus_ficheros(ws):
    c, dws = ws["c"], ws["dws"]
    c.post("/api/profiles", {"name": "dhash", "repos": {"lib": "dhash-a"}, "bases": {"lib": "main"},
                             "services": [], "worktree": True})
    install(c, "dhash")
    assert installed_now(c, "dhash")
    install(c, "dhash")
    assert not installed_now(c, "dhash"), "mismos ficheros de dependencias: no reinstala"
    c.put("/api/profiles", {"name": "dhash", "repos": {"lib": "dhash-b"}, "bases": {"lib": "main"},
                            "services": [], "worktree": True})
    install(c, "dhash")
    assert not installed_now(c, "dhash"), "cambiar de rama no reinstala si no cambian"
    wt = dws / "worktrees" / "dhash__lib"
    (wt / "pyproject.toml").write_text((wt / "pyproject.toml").read_text() + "\n# cambio\n")
    install(c, "dhash")
    assert installed_now(c, "dhash"), "cambio pyproject.toml: reinstala"
    c.delete("/api/worktrees/dhash?force=true")
    assert not (dws / "worktrees" / "dhash__lib.deps").exists(), "se borra la marca con el worktree"
    c.delete("/api/profiles/dhash")


def test_adoptar_en_perfil_nuevo_arranca_todos_los_servicios(ws):
    c, dws = ws["c"], ws["dws"]
    sh(ws["root"] / "commons" / "worker", "git", "worktree", "add", "-q", "-b", "adop2-w",
       str(dws / "worktrees" / "adop2__worker"))
    c.post("/api/worktrees/orphans/adop2__worker/adopt")
    wait_job(c, "adop2")
    assert c.get("/api/profiles/adop2")["services"]["services"] == []
    ids = [s["id"] for s in c.get("/api/state")["services"] if s.get("profile") == "adop2"]
    assert ids == ["adop2/worker", "adop2/api", "adop2/web"], "todos, en el orden de config.toml"
    c.delete("/api/worktrees/adop2")
    c.delete("/api/profiles/adop2")
