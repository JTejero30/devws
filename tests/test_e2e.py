"""Pruebas end-to-end de devws contra un workspace de mentira.

Crea en un directorio temporal repos git reales (con remoto "origin"), una copia
de .devws con su propio config.toml y arranca server.py en un puerto libre.
Los servicios de prueba devuelven su cwd, asi se comprueba desde donde corren.
Ejecutar: uv run pytest -v
"""

import json
import os
import re
import shutil
import signal
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
                status, raw, self.headers = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, raw, self.headers = e.code, e.read(), e.headers
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


def test_ui_js_sin_errores_de_sintaxis(tmp_path):
    """El JS de la UI no tiene build: un error de sintaxis deja la pagina en blanco."""
    html = (DEVWS / "static" / "index.html").read_text()
    js = tmp_path / "ui.js"
    js.write_text("\n".join(re.findall(r"<script>(.*?)</script>", html, re.S)))
    res = subprocess.run([shutil.which("node") or "node", "--check", str(js)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


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
    assert "ya está abierta en la carpeta original de api" in w["error"], w
    assert "fuera del espacio" in w["error"] and str(ws["root"]) not in w["error"], "accionable y sin rutas"
    assert not (dws / "worktrees" / "dup__api").exists()
    c.delete("/api/profiles/dup")


def test_rama_local_desfasada_se_pone_al_dia_con_origin(ws):
    """Al abrir una rama que ya existe en local (crear worktree o checkout en el
    original) se pone en lo ultimo de origin si solo le faltan commits; con
    commits propios sin subir no se toca nada."""
    c, dws, root = ws["c"], ws["dws"], ws["root"]
    web = root / "web"
    clone = root / "_clone_web"
    sh(root, "git", "clone", "-q", str(root / "_remotes" / "web.git"), str(clone))
    sh(clone, "git", "config", "user.email", "test@devws")
    sh(clone, "git", "config", "user.name", "test")
    sh(clone, "git", "checkout", "-q", "-b", "ff-x")

    def push(msg: str) -> str:
        (clone / "FF").write_text(msg)
        sh(clone, "git", "add", ".")
        sh(clone, "git", "commit", "-q", "-m", msg)
        sh(clone, "git", "push", "-q", "origin", "HEAD:ff-x")
        return git_out(clone, "rev-parse", "HEAD")

    def local_commit(msg: str) -> str:
        """Commit en la rama local ff-x del original sin hacer checkout."""
        sha = git_out(web, "commit-tree", "ff-x^{tree}", "-p", "ff-x", "-m", msg)
        sh(web, "git", "update-ref", "refs/heads/ff-x", sha)
        return sha

    v1 = push("v1")
    sh(web, "git", "fetch", "-q", "origin")
    sh(web, "git", "branch", "-q", "ff-x", "origin/ff-x")
    v2 = push("v2")  # el original aun no lo sabe: lo trae el fetch de devws

    # worktree nuevo de una rama local por detras: sale con lo ultimo de origin
    d = dws / "worktrees" / "ffwt__web"
    c.post("/api/profiles", {"name": "ffwt", "repos": {"web": "ff-x"}, "worktree": True})
    prepare(c, "ffwt")
    assert git_out(d, "rev-parse", "HEAD") == v2 != v1 and (d / "FF").read_text() == "v2"
    assert git_out(web, "rev-parse", "ff-x") == v2
    c.delete("/api/worktrees/ffwt")

    # commits propios sin subir y origin tambien avanza: se abre la local tal cual y avisa
    mine = local_commit("mio")
    push("v3")
    prepare(c, "ffwt")
    assert git_out(d, "rev-parse", "HEAD") == mine
    assert any("AVISO" in l and "commits sin subir" in l for l in c.get("/api/logs/ffwt--actividad")["lines"])
    c.delete("/api/worktrees/ffwt")

    # solo por delante de origin (nada nuevo en origin): ni se toca ni avisa
    v4 = push("v4")
    sh(web, "git", "fetch", "-q", "origin")
    sh(web, "git", "update-ref", "refs/heads/ff-x", v4)
    mine = local_commit("mio 2")
    prepare(c, "ffwt")
    assert git_out(d, "rev-parse", "HEAD") == mine
    assert not any("AVISO" in l for l in c.get("/api/logs/ffwt--actividad")["lines"])
    c.delete("/api/worktrees/ffwt")
    c.delete("/api/profiles/ffwt")

    # checkout en la carpeta original: igual
    sh(web, "git", "update-ref", "refs/heads/ff-x", v1)
    v5 = push("v5")
    try:
        [r] = c.post("/api/checkout", {"items": [{"repo": "web", "branch": "ff-x"}]})
        assert r == {"repo": "web", "ok": True, "branch": "ff-x"}, r
        assert git_out(web, "rev-parse", "HEAD") == v5 and (web / "FF").read_text() == "v5"
        c.post("/api/checkout", {"items": [{"repo": "web", "branch": "main"}]})
        mine = local_commit("mio 3")
        push("v6")
        [r] = c.post("/api/checkout", {"items": [{"repo": "web", "branch": "ff-x"}]})
        assert r["ok"] and "commits sin subir" in r["warn"], r
        assert git_out(web, "rev-parse", "HEAD") == mine
    finally:
        sh(web, "git", "checkout", "-q", "main")
        sh(web, "git", "branch", "-q", "-D", "ff-x")


def cleanup_of(c: Client, name: str) -> dict:
    return c.get("/api/cleanup")["spaces"][name]


def tags_of(c: Client, name: str, repo: str) -> list[str]:
    e = next(e for e in cleanup_of(c, name)["repos"] if e["repo"] == repo)
    return [t for _, t in e["health"]["tags"]]


def other_clone(root: Path, repo: str, name: str) -> Path:
    """Otra copia del remoto del repo: lo que hacen otros (o GitLab) en origin."""
    d = root / name
    sh(root, "git", "clone", "-q", str(root / "_remotes" / f"{repo}.git"), str(d))
    sh(d, "git", "config", "user.email", "test@devws")
    sh(d, "git", "config", "user.name", "test")
    return d


def squash_into_main(other: Path, branch: str) -> None:
    sh(other, "git", "checkout", "-q", "main")
    sh(other, "git", "pull", "-q", "origin", "main")
    sh(other, "git", "fetch", "-q", "origin")
    sh(other, "git", "merge", "-q", "--squash", f"origin/{branch}")
    sh(other, "git", "commit", "-q", "-m", f"squash {branch}")
    sh(other, "git", "push", "-q", "origin", "main")


def commit_in(d: Path, msg: str) -> None:
    (d / f"{msg}.txt").write_text(msg)
    sh(d, "git", "add", ".")
    sh(d, "git", "commit", "-q", "-m", msg)


def age_space(dws: Path, name: str, days: int) -> None:
    """Como si el espacio llevara `days` dias sin tocarse (servidor en reposo)."""
    old = time.time() - days * 86400
    st_file = dws / "state.json"
    st = json.loads(st_file.read_text())
    st.setdefault("used", {})[name] = old
    st_file.write_text(json.dumps(st))
    prof = dws / "profiles" / f"{name}.toml"
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(old))
    prof.write_text(re.sub(r'created_at = "[^"]*"', f'created_at = "{stamp}"', prof.read_text()))
    for d in (dws / "worktrees").glob(f"{name}__*"):
        gitdir = d / (d / ".git").read_text().split("gitdir:", 1)[1].strip()
        for f in [gitdir / "logs" / "HEAD", *(f for f in d.rglob("*") if ".git" not in f.parts)]:
            if f.exists() and not f.is_symlink():
                os.utime(f, (old, old))


def test_limpieza_estado_de_las_ramas_y_veredicto(ws):
    """Por repo: sin commits propios, nunca subida, sin subir, stash, sin
    mergear, mergeada con squash y borrada en origin. Y el veredicto del espacio:
    en uso, revisar (viejo con trabajo sin mergear) o se puede borrar."""
    c, dws, root = ws["c"], ws["dws"], ws["root"]
    other = other_clone(root, "commons_worker", "_clone_lmp")
    c.post("/api/profiles", {"name": "lmp", "repos": {"worker": "lmp-x"}, "bases": {"worker": "main"},
                             "worktree": True})
    prepare(c, "lmp")
    d = dws / "worktrees" / "lmp__worker"
    assert tags_of(c, "lmp", "worker") == ["sin commits propios"]
    s = cleanup_of(c, "lmp")
    assert s["verdict"] == "en_uso" and s["idle_days"] == 0 and s["reasons"] == [], s

    commit_in(d, "uno")
    assert tags_of(c, "lmp", "worker") == ["nunca subida · 1 commit"]
    sh(d, "git", "push", "-q", "-u", "origin", "lmp-x")
    assert tags_of(c, "lmp", "worker") == ["sin mergear en main"]
    commit_in(d, "dos")
    (d / "a-medias.txt").write_text("x")
    sh(d, "git", "stash", "-q", "-u")
    assert tags_of(c, "lmp", "worker") == ["1 commit sin subir", "1 stash"]
    s = cleanup_of(c, "lmp")
    assert s["verdict"] == "en_uso" and "worker: 1 commit sin subir" in s["reasons"], s
    age_space(dws, "lmp", 10)
    s = cleanup_of(c, "lmp")
    assert s["verdict"] == "revisar" and s["idle_days"] == 10, s
    assert s["reasons"][0] == "sin uso desde hace 10 días" and "worker: 1 stash" in s["reasons"], s
    sh(d, "git", "stash", "drop", "-q")
    sh(d, "git", "push", "-q")

    squash_into_main(other, "lmp-x")
    assert tags_of(c, "lmp", "worker") == ["sin mergear en main"], "sin traer lo de origin aun no se sabe"
    c.post("/api/cleanup/fetch")
    wait_job(c, "__origin")
    rep = c.get("/api/cleanup")
    assert rep["fetch_job"]["status"] == "ok" and time.time() - rep["fetched"]["at"] < 60, rep
    assert tags_of(c, "lmp", "worker") == ["mergeada en main"]
    s = rep["spaces"]["lmp"]
    assert s["verdict"] == "borrable" and s["reasons"][0] == "worker: mergeada en main", s
    # un espacio in-place tambien sobra cuando su rama esta mergeada
    c.post("/api/profiles", {"name": "lmp-orig", "repos": {"worker": "lmp-x"}})
    s = cleanup_of(c, "lmp-orig")
    assert s["verdict"] == "borrable" and s["reasons"] == ["worker: mergeada en main"], s
    c.delete("/api/profiles/lmp-orig")

    # GitLab borra la rama al mergear y se sigue trabajando en ella sin subir
    sh(other, "git", "push", "-q", "origin", "--delete", "lmp-x")
    c.post("/api/cleanup/fetch")
    wait_job(c, "__origin")
    assert tags_of(c, "lmp", "worker") == ["mergeada en main"], "borrada pero mergeada: nada que avisar"
    commit_in(d, "tres")
    assert tags_of(c, "lmp", "worker") == ["borrada en origin · sin mergear"]

    # sin nada mergeado no se puede borrar aunque no tenga trabajo: viejo, a revisar
    c.post("/api/profiles", {"name": "lmp2", "repos": {"web": "lmp2-x"}, "bases": {"web": "main"},
                             "worktree": True})
    prepare(c, "lmp2")
    assert cleanup_of(c, "lmp2")["verdict"] == "en_uso"
    age_space(dws, "lmp2", 8)
    s = cleanup_of(c, "lmp2")
    assert s["verdict"] == "revisar" and s["reasons"] == ["sin uso desde hace 8 días", "web: sin commits propios"], s
    c.post("/api/profiles", {"name": "lmp3", "repos": {"api": "main"}})
    age_space(dws, "lmp3", 30)
    s = cleanup_of(c, "lmp3")
    assert s["verdict"] == "revisar" and s["reasons"] == ["sin uso desde hace 30 días", "api: al día con origin"], s
    c.delete("/api/profiles/lmp3")

    for name in ("lmp", "lmp2"):
        c.delete(f"/api/worktrees/{name}?force=true")
        c.delete(f"/api/profiles/{name}")
    sh(root / "commons" / "worker", "git", "branch", "-q", "-D", "lmp-x")
    sh(root / "web", "git", "branch", "-q", "-D", "lmp2-x")


def test_eliminar_espacio_borra_sus_ramas_y_nada_mas(ws):
    """Al eliminar un espacio se borran las ramas que devws creo para el: la
    local si no se pierde nada y la de origin si la creo el y esta mergeada.
    Nunca una protegida, ni la de otro espacio, ni la de origin que ya existia,
    ni lo que no se confirmo."""
    c, dws, root = ws["c"], ws["dws"], ws["root"]
    worker, api, web, lib = root / "commons" / "worker", root / "api", root / "web", root / "lib"
    other = other_clone(root, "commons_worker", "_clone_brr")
    repos = {"worker": "brr-y", "api": "feat-a", "web": "desa", "lib": "brr-wip"}
    bases = {"worker": "main", "web": "main", "lib": "main"}
    c.post("/api/profiles", {"name": "brr", "repos": repos, "bases": bases, "worktree": True})
    prepare(c, "brr")
    # el espacio pasa worker a otra rama: las dos son suyas
    c.put("/api/profiles", {"name": "brr", "repos": {**repos, "worker": "brr-x"}, "bases": bases,
                            "worktree": True})
    prepare(c, "brr")
    c.post("/api/profiles", {"name": "brr-otro", "repos": {"worker": "brr-y"}})
    wd = dws / "worktrees" / "brr__worker"
    commit_in(wd, "x")
    sh(wd, "git", "push", "-q", "-u", "origin", "brr-x")
    squash_into_main(other, "brr-x")
    commit_in(dws / "worktrees" / "brr__lib", "wip")
    (dws / "logs" / "brr--worker.log").write_text("log\n")

    plan = c.post("/api/cleanup/plan", {"spaces": ["brr"]})
    assert plan["offline"] == [] and len(plan["targets"]) == 1
    t = plan["targets"][0]
    assert sorted(x["repo"] for x in t["dirs"]) == ["api", "lib", "web", "worker"]
    got = {(e["repo"], e["branch"]): (e["local"], e["remote"], e["keep_local"], e["keep_remote"])
           for e in t["branches"]}
    assert got == {
        ("worker", "brr-x"): (True, True, None, None),
        ("worker", "brr-y"): (False, False, "la usa el espacio brr-otro", None),
        ("api", "feat-a"): (True, False, None, None),  # su origin/feat-a ya existia: no es del espacio
        ("web", "desa"): (False, False, "rama protegida", None),
        ("lib", "brr-wip"): (False, False, "1 commit que solo está en local", None),
    }, got

    # se confirma todo lo que el plan borra menos la de api
    confirmed = [{"repo": e["repo"], "branch": e["branch"], "local": e["local"], "remote": e["remote"]}
                 for e in t["branches"] if e["repo"] != "api"]
    r = c.post("/api/cleanup/remove", {"spaces": ["brr"], "branches": confirmed})
    assert r["ok"] and sorted(r["deleted"]) == ["brr-x (worker)", "origin/brr-x (worker)"], r
    assert {"feat-a (api)", "brr-y (worker): la usa el espacio brr-otro", "desa (web): rama protegida",
            "brr-wip (lib): 1 commit que solo está en local"} <= set(r["kept"]), r
    assert "brr" not in c.get("/api/profiles") and not list((dws / "worktrees").glob("brr__*"))
    assert not (dws / "logs" / "brr--worker.log").exists()
    assert "brr" not in json.loads((dws / "state.json").read_text()).get("branches", {})
    assert git_out(worker, "branch", "--list", "brr-x") == ""
    assert git_out(worker, "ls-remote", "--heads", "origin", "brr-x") == ""
    assert git_out(api, "ls-remote", "--heads", "origin", "feat-a") != "", "la de origin ya existia"
    assert git_out(api, "branch", "--list", "feat-a") != "", "no confirmada: se conserva"
    for d, b in ((worker, "brr-y"), (web, "desa"), (lib, "brr-wip")):
        assert git_out(d, "branch", "--list", b) != "", b
        sh(d, "git", "branch", "-q", "-D", b)
    c.delete("/api/profiles/brr-otro")

    # worktree suelto (repo quitado de un espacio): se borra con su rama
    c.post("/api/profiles", {"name": "brr2", "repos": {"lib": "brr2-x", "web": "brr2-x"},
                             "bases": {"lib": "main", "web": "main"}, "worktree": True})
    prepare(c, "brr2")
    c.put("/api/profiles", {"name": "brr2", "repos": {"web": "brr2-x"}, "bases": {"web": "main"},
                            "worktree": True})
    assert [o["dir"] for o in c.get("/api/cleanup")["orphans"]] == ["brr2__lib"]
    t = c.post("/api/cleanup/plan", {"orphans": ["brr2__lib"]})["targets"][0]
    assert [(e["branch"], e["local"]) for e in t["branches"]] == [("brr2-x", True)], t
    r = c.post("/api/cleanup/remove", {"orphans": ["brr2__lib"], "branches": [
        {"repo": "lib", "branch": "brr2-x", "local": True}]})
    assert r["deleted"] == ["brr2-x (lib)"], r
    assert git_out(lib, "branch", "--list", "brr2-x") == ""
    assert c.get("/api/cleanup")["orphans"] == []
    r = c.post("/api/cleanup/remove", {"spaces": ["brr2"], "branches": [
        {"repo": "web", "branch": "brr2-x", "local": True}]})
    assert r["deleted"] == ["brr2-x (web)"], r


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
    assert status == 409 and "está arrancado ea" in out["detail"]
    assert c.headers["X-Devws-Code"] == "other_space_running", "la UI lo reconoce por el codigo"
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
    assert "ya está abierta" in j["error"] and j["finished"], "con la hora a la que acabo"
    assert c.get("/api/state")["active_profile"] is None
    assert all(s["status"] == "down" for s in c.get("/api/state")["services"]), "no arranca servicios si falla"
    c.delete("/api/profiles/roto")


def test_repos_dicen_donde_esta_abierta_cada_rama(ws):
    c, root, dws = ws["c"], ws["root"], ws["dws"]
    c.post("/api/profiles", {"name": "abiertas", "repos": {"api": "feat-a"}, "worktree": True})
    prepare(c, "abiertas")
    api = {r["name"]: r for r in c.get("/api/repos")}["api"]
    assert api["open"]["main"] == {"where": "original"}
    assert api["open"]["feat-a"] == {"where": "space", "space": "abiertas"}
    assert api["worktrees"] == {"abiertas": {"branch": "feat-a", "dirty": 0}}
    (dws / "worktrees" / "abiertas__api" / "sucio.txt").write_text("x")
    assert {r["name"]: r for r in c.get("/api/repos")}["api"]["worktrees"]["abiertas"]["dirty"] == 1

    # checkout en el original de una rama abierta en un worktree: en español y sin rutas
    [r] = c.post("/api/checkout", {"items": [{"repo": "api", "branch": "feat-a"}]})
    assert not r["ok"] and r["error"] == "la rama feat-a ya está abierta en el espacio abiertas", r
    # original con cambios sin commitear
    (root / "api" / "sucio.txt").write_text("x")
    try:
        [r] = c.post("/api/checkout", {"items": [{"repo": "api", "branch": "otra"}]})
        assert not r["ok"] and "cambios sin commitear en su carpeta original" in r["error"], r
    finally:
        (root / "api" / "sucio.txt").unlink()
    c.delete("/api/worktrees/abiertas?force=true")
    c.delete("/api/profiles/abiertas")


def test_servicio_caido_y_puerto_en_uso_por_otro_espacio(ws):
    c = ws["c"]
    c.post("/api/profiles", {"name": "vecino", "repos": {"worker": "feat-a"}, "services": ["api"], "worktree": True})
    prepare(c, "vecino")
    c.post("/api/profiles", {"name": "caida", "repos": {}, "services": ["api"], "worktree": False})
    c.post("/api/profiles/caida/start")
    wait_job(c, "caida")
    s = svc(c, "vecino/api")
    assert s["status"] == "other_space" and s["owner"] == "caida", "no es algo de fuera: es otro espacio"

    os.kill(svc(c, "api")["pid"], signal.SIGKILL)
    s = wait_for(lambda: (s := svc(c, "api"))["status"] == "crashed" and s, 15, "api caido")
    assert "last_line" in s, "se ve por que cayo"
    assert svc(c, "vecino/api")["status"] == "down"
    c.post("/api/profiles/caida/stop")
    wait_job(c, "caida")
    assert svc(c, "api")["status"] == "down", "parar el espacio olvida sus caidos"
    c.delete("/api/worktrees/vecino")
    for p in ("vecino", "caida"):
        c.delete(f"/api/profiles/{p}")


def test_worktree_npm_sin_dependencias_arranca(ws):
    """npm install sin dependencias no crea node_modules: no por eso faltan."""
    c, ports, dws = ws["c"], ws["ports"], ws["dws"]
    c.post("/api/profiles", {"name": "npmsin", "repos": {"web": "feat-a"}, "services": ["web"], "worktree": True})
    c.post("/api/profiles/npmsin/start")
    wait_job(c, "npmsin")
    assert fetch(ports["web"]) == str(dws / "worktrees" / "npmsin__web")
    c.post("/api/profiles/npmsin/stop")
    wait_job(c, "npmsin")
    c.delete("/api/worktrees/npmsin?force=true")  # npm install deja un package-lock.json sin versionar
    c.delete("/api/profiles/npmsin")


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
            "devws_remove_workspace", "devws_add_repo", "devws_configure_startup", "devws_install_deps",
            "devws_repo_config", "devws_update_repo"} <= tools

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
    err, st = mcp_tool(c, "devws_status")
    mcp1 = next(s for s in st["spaces"] if s["name"] == "mcp1")
    assert mcp1["cleanup"]["verdict"] == "en_uso" and mcp1["cleanup"]["idle_days"] == 0, mcp1
    err, r = mcp_tool(c, "devws_remove_workspace", name="mcp1", force=True)
    assert not err and r["ok"], r
    assert not (dws / "worktrees" / "mcp1__api").exists()
    assert "mcp1" not in c.get("/api/profiles")
    # sus ramas locales se borran (nada se pierde); la de origin ya existia: se queda
    assert sorted(r["deleted"]) == ["feat-a (api)", "mcp-nueva (worker)"], r
    assert git_out(ws["root"] / "commons" / "worker", "branch", "--list", "mcp-nueva") == ""
    assert git_out(ws["root"] / "api", "ls-remote", "--heads", "origin", "feat-a") != ""


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


def test_ver_y_editar_la_configuracion_de_un_repo(ws):
    c, root, dws = ws["c"], ws["root"], ws["dws"]
    cfg = dws / "config.toml"
    before = cfg.read_text()
    py, port, port2 = sys.executable, free_port(), free_port()
    cmd = [py, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "{port}"]
    c.post("/api/repos", {"name": "edt", "path": str(root / "api"),
                          "service": {"name": "edt", "port": port, "cmd": cmd}})
    conf = c.get("/api/repos/edt/config")
    assert conf["services"] == [{"name": "edt", "port": port, "cmd": cmd}]
    assert conf["install"] == ["uv", "sync"] and conf["install_is_default"] and conf["has_dotenv"]

    # cmd escrito a mano en varias lineas y con comentarios: se edita sin romper nada
    text = cfg.read_text()
    one_line = "cmd = [" + ", ".join(json.dumps(a) for a in cmd) + "]\n"
    assert one_line in text
    head, _, tail = text.rpartition(one_line)  # el ultimo: el servicio api tiene el mismo cmd
    cfg.write_text(head + "# arranque a mano\ncmd = [\n" + "".join(
        f"  {json.dumps(a)},\n" for a in cmd) + "]  # fin\n" + tail)
    ws["restart"]()

    r = c.put("/api/repos/edt", {"services": [{"name": "edt", "port": port2, "cmd": cmd[:-1] + ["{port}", "--reload"]}],
                                 "install": "uv sync --frozen", "copy_files": [".env"]})
    assert r["services"][0]["port"] == port2 and r["services"][0]["cmd"][-1] == "--reload"
    assert r["install"] == ["uv", "sync", "--frozen"] and not r["install_is_default"]
    assert r["copy"] == [".env"]
    text = cfg.read_text()
    assert "# arranque a mano" in text and "  # fin" not in text, "se conserva el comentario de fuera"
    assert tomllib.loads(text)["services"][-1]["cmd"] == cmd[:-1] + ["{port}", "--reload"]

    r = c.put("/api/repos/edt", {"install": "uv sync", "copy_files": []})
    assert r["install_is_default"] and r["copy"] == [], "el de por defecto y la lista vacia no se escriben"
    blk = next(x for x in tomllib.loads(cfg.read_text())["repos"] if x["name"] == "edt")
    assert "install" not in blk and "copy" not in blk

    st, out = c.put("/api/repos/edt", {"services": [{"name": "edt", "port": ws["ports"]["api"], "cmd": cmd}]}, ok=False)
    assert st == 409 and "ya lo usa el servicio api" in out["detail"]
    st, out = c.put("/api/repos/edt", {"services": [{"name": "otro", "port": port, "cmd": cmd}]}, ok=False)
    assert st == 404
    st, out = c.put("/api/repos/edt", {"path": "relativo"}, ok=False)
    assert st == 400
    r = c.put("/api/repos/edt", {"path": str(root / "web"), "kind": "npm"})
    assert r["resolved_path"] == str((root / "web").resolve()) and r["install"][0] == "npm"

    r = c.delete("/api/repos/edt/startup")
    assert r["services"] == [] and r["library"] is None
    assert {x["name"]: x for x in c.get("/api/repos")}["edt"]["startup"] is None
    c.delete("/api/repos/edt")
    assert tomllib.loads(cfg.read_text()) == tomllib.loads(before)


def test_mcp_ve_y_edita_la_configuracion_de_un_repo(ws):
    c = ws["c"]
    err, conf = mcp_tool(c, "devws_repo_config", repo="api")
    assert not err and conf["services"][0]["name"] == "api" and conf["copy"] == [".env"]
    err, r = mcp_tool(c, "devws_update_repo", repo="api", install=["uv", "sync", "--frozen"])
    assert not err and r["install"] == ["uv", "sync", "--frozen"], r
    err, r = mcp_tool(c, "devws_update_repo", repo="api", install=["uv", "sync"])
    assert not err and r["install_is_default"]
    err, msg = mcp_tool(c, "devws_repo_config", repo="nope")
    assert err and "repo desconocido" in msg


def test_logs_incrementales_y_escritura(ws):
    c, logs = ws["c"], ws["dws"] / "logs"
    logs.mkdir(exist_ok=True)
    f = logs / "term.log"
    f.write_bytes(b"=== up term ===\n" + b"".join(f"linea {i}\n".encode() for i in range(300)) + b"a medi")
    q = lambda **kw: c.get("/api/logs/term?" + urllib.parse.urlencode(kw))

    r = q()
    assert r["lines"] == [f"linea {i}" for i in range(150, 300)] and not r["reset"]
    assert r["gen"] == "=== up term ===" and r["partial"] == "a medi"
    assert r["offset"] == f.stat().st_size - len("a medi")
    assert len(q(lines=1000)["lines"]) == 301

    # incremental: solo lineas completas nuevas; la parcial se completa
    off, gen = r["offset"], r["gen"]
    r = q(offset=off, gen=gen)
    assert r["lines"] == [] and r["offset"] == off and r["partial"] == "a medi" and not r["reset"]
    with open(f, "ab") as fh:
        fh.write(b"o\nnueva\n")
    r = q(offset=off, gen=gen)
    assert r["lines"] == ["a medio", "nueva"] and r["partial"] == "" and not r["reset"]
    assert r["offset"] == f.stat().st_size

    # escribir desde la terminal
    off = r["offset"]
    c.post("/api/logs/term/write", {"text": "hola"})
    c.post("/api/logs/term/write", {"text": ""})
    r = q(offset=off, gen=gen)
    assert r["lines"] == ["hola", ""] and not r["reset"]
    st, _ = c.post("/api/logs/term/write", {"text": "x" * 4001}, ok=False)
    assert st == 400
    st, _ = c.post("/api/logs/no-existe/write", {"text": "hola"}, ok=False)
    assert st == 404
    st, _ = c.post("/api/logs/wt/nadie/term/write", {"text": "hola"}, ok=False)
    assert st == 404
    assert c.get("/api/logs/wt/nadie/term")["lines"] == []

    # log reescrito (servicio rearrancado): reset y cola
    f.write_bytes(b"=== up term otra vez ===\ncorta\n")
    r = q(offset=off, gen=gen)
    assert r["reset"] and r["lines"] == ["=== up term otra vez ===", "corta"]
    r = q(offset=10 ** 9, gen=r["gen"])
    assert r["reset"] and r["lines"][-1] == "corta"
    f.unlink()


def pid_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    # zombi (hijo del server sin recoger): cuenta como muerto
    stat = Path(f"/proc/{pid}/stat")
    return stat.exists() and stat.read_text().split(")")[-1].split()[0] == "Z"


def test_parar_corta_el_arranque_mientras_instala(ws):
    c, root, dws = ws["c"], ws["root"], ws["dws"]
    cfg = dws / "config.toml"
    before = cfg.read_text()
    pidf = root / "instalador.pid"
    inst = [sys.executable, "-c",
            f"import os, time; open({str(pidf)!r}, 'w').write(str(os.getpid())); time.sleep(120)"]
    c.post("/api/repos", {"name": "lento", "path": str(root / "lib"), "install": inst, "library": []})
    c.post("/api/profiles", {"name": "corta", "repos": {"lento": "corta-x"}, "bases": {"lento": "main"},
                             "services": ["api"], "worktree": True})
    c.post("/api/profiles/corta/start")
    wait_for(lambda: pidf.exists() and pidf.read_text(), 60, "instalador en marcha")
    pid = int(pidf.read_text())
    t0 = time.time()
    c.post("/api/profiles/corta/stop")
    j = wait_job(c, "corta")
    assert j["action"] == "stop" and time.time() - t0 < 30, "parar no espera a que acabe la instalacion"
    wait_for(lambda: pid_dead(pid), 10, "instalador muerto")
    assert svc(c, "api")["status"] == "down", "no siguio arrancando servicios"
    log = c.get("/api/logs/corta--actividad")["lines"]
    assert any("cortado para parar" in l for l in log) and any("=== stop corta" in l for l in log)
    # cortado a medias: se puede volver a trabajar con el espacio
    c.delete("/api/worktrees/corta?force=true")
    c.delete("/api/profiles/corta")
    c.delete("/api/repos/lento")
    assert tomllib.loads(cfg.read_text()) == tomllib.loads(before)


def test_parar_mientras_un_servicio_espera_su_puerto(ws):
    c, root, dws = ws["c"], ws["root"], ws["dws"]
    cfg = dws / "config.toml"
    before = cfg.read_text()
    port = free_port()
    c.post("/api/repos", {"name": "colgado", "path": str(root / "api"), "service": {
        "name": "colgado", "port": port, "cmd": [sys.executable, "-c", "import time; time.sleep(600)"]}})
    c.post("/api/profiles", {"name": "cuelga", "repos": {}, "services": ["colgado"], "worktree": False})
    c.post("/api/profiles/cuelga/start")
    s = wait_for(lambda: (s := svc(c, "colgado"))["status"] == "starting" and s, 30, "colgado arrancando")
    # mientras arranca, otro trabajo no puede empezar, pero parar si
    st, out = c.post("/api/profiles/cuelga/start?force=true", ok=False)
    assert st == 409 and "espera" in out["detail"]
    t0 = time.time()
    c.post("/api/profiles/cuelga/stop")
    wait_job(c, "cuelga")
    assert time.time() - t0 < 30, "no espera los 90 s del puerto"
    assert svc(c, "colgado")["status"] == "down"
    wait_for(lambda: pid_dead(s["pid"]), 10, "proceso colgado muerto")
    c.delete("/api/profiles/cuelga")
    c.delete("/api/repos/colgado")
    assert tomllib.loads(cfg.read_text()) == tomllib.loads(before)


def test_servicio_que_muere_al_arrancar_no_hace_esperar(ws):
    c, root, dws = ws["c"], ws["root"], ws["dws"]
    cfg = dws / "config.toml"
    before = cfg.read_text()
    c.post("/api/repos", {"name": "muere", "path": str(root / "api"), "service": {
        "name": "muere", "port": free_port(), "cmd": [sys.executable, "-c", "import sys; sys.exit(3)"]}})
    t0 = time.time()
    r = c.post("/api/services/muere/up")
    assert time.time() - t0 < 20 and "termino (codigo 3)" in r["warn"], r
    c.delete("/api/repos/muere")
    assert tomllib.loads(cfg.read_text()) == tomllib.loads(before)


def test_solo_lectura_misma_rama_en_varios_espacios(ws):
    """Un repo en solo lectura queda detached en lo ultimo de su rama: varios
    espacios pueden usar la misma rama (p. ej. desa) a la vez."""
    c, dws, root = ws["c"], ws["dws"], ws["root"]
    clone = root / "_clone_desa"
    sh(root, "git", "clone", "-q", str(root / "_remotes" / "commons_worker.git"), str(clone))
    sh(clone, "git", "config", "user.email", "test@devws")
    sh(clone, "git", "config", "user.name", "test")

    def push_desa(msg: str) -> str:
        (clone / "DESA").write_text(msg)
        sh(clone, "git", "add", ".")
        sh(clone, "git", "commit", "-q", "-m", msg)
        sh(clone, "git", "push", "-q", "origin", "HEAD:desa")
        return git_out(clone, "rev-parse", "HEAD")

    sh(clone, "git", "checkout", "-q", "-b", "desa")
    v1 = push_desa("v1")
    status, out = c.post("/api/profiles", {"name": "roip", "repos": {"worker": "desa"},
                                           "readonly": ["worker"]}, ok=False)
    assert status == 400 and "solo lectura" in out["detail"], "solo en espacios worktree"

    ro1, ro2 = dws / "worktrees" / "ro1__worker", dws / "worktrees" / "ro2__worker"
    for p in ("ro1", "ro2"):
        c.post("/api/profiles", {"name": p, "repos": {"worker": "desa"}, "readonly": ["worker"],
                                 "worktree": True})
        w = prepare(c, p)
        assert w["pending"] == [] and w["readonly"] == ["worker"], w
    for d in (ro1, ro2):
        assert git_out(d, "branch", "--show-current") == "", "detached: la rama no se abre"
        assert git_out(d, "rev-parse", "HEAD") == v1 and (d / "DESA").read_text() == "v1"
    assert "readonly" in c.get("/api/profiles/ro1")

    # con la rama libre, otro espacio puede ademas abrirla para desarrollar en ella
    c.post("/api/profiles", {"name": "rodev", "repos": {"worker": "desa"}, "worktree": True})
    prepare(c, "rodev")
    assert git_out(dws / "worktrees" / "rodev__worker", "branch", "--show-current") == "desa"
    c.delete("/api/worktrees/rodev")
    c.delete("/api/profiles/rodev")

    # avanza origin/desa: preparar lo trae (tras el fetch se ve pendiente)
    v2 = push_desa("v2")
    w = prepare(c, "ro1")
    assert git_out(ro1, "rev-parse", "HEAD") == v2 and w["pending"] == []
    assert wt_info(c, "ro2")["pending"] == ["worker"], "ro2 aun en v1: se actualiza al arrancar"

    # con cambios sin commitear no se mueve ni falla: se avisa en el log
    (ro2 / "local.txt").write_text("x")
    prepare(c, "ro2")
    assert git_out(ro2, "rev-parse", "HEAD") == v1
    assert any("AVISO" in l and "cambios locales" in l for l in c.get("/api/logs/ro2--actividad")["lines"])
    (ro2 / "local.txt").unlink()
    prepare(c, "ro2")
    assert git_out(ro2, "rev-parse", "HEAD") == v2

    # pasar a una rama normal y volver a solo lectura
    c.put("/api/profiles", {"name": "ro1", "repos": {"worker": "ro-nueva"}, "bases": {"worker": "desa"},
                            "worktree": True})
    prepare(c, "ro1")
    assert git_out(ro1, "branch", "--show-current") == "ro-nueva"
    c.put("/api/profiles", {"name": "ro1", "repos": {"worker": "desa"}, "readonly": ["worker"],
                            "worktree": True})
    prepare(c, "ro1")
    assert git_out(ro1, "branch", "--show-current") == "" and git_out(ro1, "rev-parse", "HEAD") == v2

    # rama que no existe: en solo lectura no se crea
    c.put("/api/profiles", {"name": "ro1", "repos": {"worker": "nope"}, "readonly": ["worker"],
                            "worktree": True})
    assert "no existe la rama nope" in prepare(c, "ro1", expect="error")["error"]
    c.delete("/api/worktrees/ro1")
    c.delete("/api/profiles/ro1")

    # un worktree detached huerfano se adopta en solo lectura de su rama
    c.delete("/api/profiles/ro2")
    orphans = c.get("/api/worktrees/orphans")
    assert [(o["dir"], o["branch"], o["follows"]) for o in orphans] == [("ro2__worker", None, "desa")]
    c.post("/api/worktrees/orphans/ro2__worker/adopt")
    w = wait_job(c, "ro2")
    assert w["readonly"] == ["worker"] and w["repos"] == {"worker": "desa"}
    c.delete("/api/worktrees/ro2")
    c.delete("/api/profiles/ro2")

    # MCP: readonly en devws_create_workspace
    err, w = mcp_tool(c, "devws_create_workspace", name="romcp", repos={"worker": "desa"}, readonly=["worker"])
    assert not err and w["status"] == "ok" and w["readonly"] == ["worker"], w
    assert git_out(Path(w["dirs"]["worker"]), "rev-parse", "HEAD") == v2
    err, r = mcp_tool(c, "devws_remove_workspace", name="romcp")
    assert not err and r["ok"], r


def test_entornos_fijos(ws):
    """Un entorno fijo (p. ej. inte) levanta sus servicios desde worktrees en lo
    ultimo de origin/<rama> (con excepciones por repo) y dice por repo si esta al
    dia con origin o cuantos commits nuevos tiene."""
    c, dws, root, ports = ws["c"], ws["dws"], ws["root"], ws["ports"]
    clone = root / "_clone_inte"
    sh(root, "git", "clone", "-q", str(root / "_remotes" / "commons_worker.git"), str(clone))
    sh(clone, "git", "config", "user.email", "test@devws")
    sh(clone, "git", "config", "user.name", "test")
    sh(clone, "git", "checkout", "-q", "-b", "inte")

    def push_inte(msg: str) -> str:
        (clone / "INTE").write_text(msg)
        sh(clone, "git", "add", ".")
        sh(clone, "git", "commit", "-q", "-m", msg)
        sh(clone, "git", "push", "-q", "origin", "HEAD:inte")
        return git_out(clone, "rev-parse", "HEAD")

    v1 = push_inte("v1")
    for bad, why in (({"env_branch": " "}, "rama invalida"), ({"env_branch": "inte", "services": ["nope"]},
                                                              "servicio desconocido")):
        status, out = c.post("/api/profiles", {"name": "inte", "repos": {}, **bad}, ok=False)
        assert status == 400 and why in out["detail"], out

    # rama del entorno para todos sus repos; api con una excepcion
    c.post("/api/profiles", {"name": "inte", "env_branch": "inte", "services": ["worker", "api"],
                             "repos": {"api": "feat-a"}})
    p = c.get("/api/profiles/inte")
    assert p["worktree"] and p["env_branch"] == "inte", p
    assert p["repos"] == {"worker": "inte", "api": "feat-a"} and p["readonly"] == ["worker", "api"], p
    assert p["services"]["services"] == ["worker", "api"]
    env = c.get("/api/state")["environments"]["inte"]
    assert env["branch"] == "inte" and env["repos"]["worker"]["behind"] is None, "sin worktree aun"
    assert "inte" not in c.get("/api/cleanup")["spaces"], "un entorno no sobra nunca"
    status, out = c.put("/api/profiles", {"name": "inte", "repos": {"worker": "inte"}, "worktree": True}, ok=False)
    assert status == 409 and "es un entorno" in out["detail"], "no se convierte en espacio"
    err, msg = mcp_tool(c, "devws_create_workspace", name="inte", repos={"worker": "inte"})
    assert err and "entorno" in msg

    # levantar: worktrees detached en lo ultimo de origin y servicios desde ellos
    c.post("/api/profiles/inte/start?force=true")
    wait_job(c, "inte")
    wt_worker, wt_api = dws / "worktrees" / "inte__worker", dws / "worktrees" / "inte__api"
    assert git_out(wt_worker, "branch", "--show-current") == "" and git_out(wt_worker, "rev-parse", "HEAD") == v1
    assert (wt_api / "BRANCH").read_text() == "feat-a"
    assert fetch(ports["worker"]) == str(wt_worker) and fetch(ports["api"]) == str(wt_api)
    env = c.get("/api/state")["environments"]["inte"]
    assert {r: (e["behind"], e["missing"]) for r, e in env["repos"].items()} == {"worker": (0, False), "api": (0, False)}
    assert env["fetched_at"], "origin se consulto al levantarlo"

    # origin avanza: tras comprobar con origin se ve el commit nuevo; levantar de nuevo lo trae
    v2 = push_inte("v2")
    c.post("/api/cleanup/fetch")
    wait_job(c, "__origin")
    assert c.get("/api/state")["environments"]["inte"]["repos"]["worker"]["behind"] == 1
    err, st = mcp_tool(c, "devws_status")
    e = next(x for x in st["environments"] if x["name"] == "inte")
    assert e["branch"] == "inte" and e["active"] and e["repos"]["worker"]["behind"] == 1, e
    assert "inte" not in [s["name"] for s in st["spaces"]]
    err, w = mcp_tool(c, "devws_start_environment", name="inte")
    assert not err and w["status"] == "ok" and w["repos"]["worker"]["behind"] == 0, w
    assert git_out(wt_worker, "rev-parse", "HEAD") == v2
    assert all(s["status"] == "up" for s in w["services"]), w

    # cambiar la rama del entorno: el worktree pasa a la nueva (no se pierde nada)
    c.put("/api/profiles", {"name": "inte", "env_branch": "feat-a", "repos": {}, "services": ["worker", "api"]})
    assert c.get("/api/profiles/inte")["repos"] == {"worker": "feat-a", "api": "feat-a"}
    err, w = mcp_tool(c, "devws_start_environment", name="inte")
    assert not err and w["status"] == "ok", w
    assert (wt_worker / "BRANCH").read_text() == "feat-a"
    c.put("/api/profiles", {"name": "inte", "env_branch": "no-existe", "repos": {}, "services": ["worker"]})
    env = c.get("/api/state")["environments"]["inte"]
    assert env["repos"] == {"worker": {"branch": "no-existe", "behind": None, "missing": True}}, env

    err, w = mcp_tool(c, "devws_stop_environment", name="inte")
    assert not err and not w["active"] and w["services"][0]["status"] == "down", w
    err, msg = mcp_tool(c, "devws_start_environment", name="nope")
    assert err and "no existe el entorno" in msg

    # eliminarlo borra sus carpetas; ninguna rama
    plan = c.post("/api/cleanup/plan", {"spaces": ["inte"]})
    assert plan["targets"][0]["branches"] == [], plan
    r = c.post("/api/cleanup/remove", {"spaces": ["inte"]})
    assert r["ok"] and r["deleted"] == [], r
    assert not wt_worker.exists() and not wt_api.exists() and "inte" not in c.get("/api/profiles")
    assert git_out(root / "commons" / "worker", "ls-remote", "--heads", "origin", "inte") != ""
