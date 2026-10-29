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
    for name, content in {**files, ".gitignore": ".env\n.venv/\nnode_modules/\n__pycache__/\n"}.items():
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
    log = open(root / "server.log", "wb")
    proc = subprocess.Popen([sys.executable, str(dws / "server.py")], cwd=dws,
                            stdout=log, stderr=subprocess.STDOUT, env=env)
    c = Client(ports["web_ui"])
    wait_for(lambda: fetch(ports["web_ui"]) is not None, 30, "server devws")
    yield {"root": root, "dws": dws, "ports": ports, "c": c}
    for s in c.get("/api/state")["services"]:
        if s["status"] in ("up", "starting"):
            c.post(f"/api/services/{urllib.parse.quote(s['id'], safe='')}/down", ok=False)
    proc.terminate()
    proc.wait(10)
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


def test_worktree_ciclo(ws):
    c, ports, dws = ws["c"], ws["ports"], ws["dws"]
    c.post("/api/profiles", {"name": "wt1", "repos": {"api": "feat-a", "worker": "feat-a"},
                             "services": ["worker", "api"], "worktree": True})
    r = c.post("/api/profiles/wt1/apply")
    assert all(i["ok"] for i in r["checkout"]), r
    wt_api = dws / "worktrees" / "wt1__api"
    assert (wt_api / "BRANCH").read_text() == "feat-a"
    wait_for(lambda: c.get("/api/state")["worktrees"]["wt1"]["ready"], 120, "deps del worktree")
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
    r = c.post("/api/profiles/orden/apply")
    assert [n for n, _ in r["services"]] == ["api", "worker"], r
    assert all(u["ok"] for _, u in r["services"]), r
    assert c.get("/api/state")["active_profile"] == "orden"
    for s in ("api", "worker"):
        c.post(f"/api/services/{s}/down")
    assert c.get("/api/state")["active_profile"] is None
    c.delete("/api/profiles/orden")
