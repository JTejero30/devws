"""Front local multi-repo: ramas, servicios y perfiles de trabajo."""

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path

import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
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
app = FastAPI()


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
    """load → modificar → save bajo _lock, para no pisar cambios de otros hilos.
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
            return (WORKSPACE_ROOT / r["path"]).resolve()
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
    CLOSED de un uvicorn zombi): no aceptan conexiones pero sí bloquean el
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


class RepoIn(BaseModel):
    name: str
    path: str
    kind: str = "python"


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
    d = (WORKSPACE_ROOT / path).resolve()
    if not (d / ".git").exists():
        raise HTTPException(400, f"no hay repo git en {path} (relativo a {WORKSPACE_ROOT})")
    with _lock:
        cfg_file = ROOT / "config.toml"
        lines = cfg_file.read_text().splitlines(keepends=True)
        block = f"[[repos]]\nname = {toml_str(name)}\npath = {toml_str(path)}\nkind = {toml_str(kind)}\n\n"
        try:
            i = next(j for j, l in enumerate(lines) if l.lstrip().startswith("[[services]]"))
        except StopIteration:
            raise HTTPException(400, "config.toml sin seccion [[services]]")
        lines.insert(i, "\n" if lines[i - 1].strip() else "")
        lines.insert(i + 1, block)
        tmp = cfg_file.with_suffix(".toml.tmp")
        tmp.write_text("".join(lines))
        tomllib.load(open(tmp, "rb"))
        tmp.replace(cfg_file)
        CONFIG.clear()
        CONFIG.update(tomllib.load(open(cfg_file, "rb")))
    return {"ok": True, "name": name}


@app.delete("/api/repos/{name}")
def api_repos_del(name: str):
    if name not in {r["name"] for r in repos()}:
        raise HTTPException(404, "repo desconocido")
    in_use = [s["name"] for s in services() if s["repo"] == name]
    if in_use:
        raise HTTPException(409, f"tiene servicios asociados: {', '.join(in_use)}")
    used = {prof for prof, w in wt_profiles().items() if name in w["repos"]}
    if used:
        raise HTTPException(409, f"en uso por perfil(s): {', '.join(sorted(used))}")
    with _lock:
        cfg_file = ROOT / "config.toml"
        lines = cfg_file.read_text().splitlines(keepends=True)
        # un bloque [[repos]] termina en el siguiente header de seccion [...] o en otro [[repos]]
        def next_header(i: int) -> int:
            for j in range(i + 1, len(lines)):
                t = lines[j].strip()
                if t.startswith("[") and not t.startswith("#"):
                    return j
            return len(lines)
        starts = [i for i, l in enumerate(lines) if l.strip() == "[[repos]]"]
        ends = [next_header(s) for s in starts]
        removed = None
        for k, s in enumerate(starts):
            if any(l.strip().startswith(f'name = "{name}"') for l in lines[s:ends[k]]):
                removed = k
                break
        if removed is None:
            raise HTTPException(404, "no se encontro el bloque del repo en config.toml")
        s, e = starts[removed], ends[removed]
        if s > 0 and not lines[s - 1].strip():
            s -= 1
        if e < len(lines) and not lines[e].strip():
            e += 1
        del lines[s:e]
        tmp = cfg_file.with_suffix(".toml.tmp")
        tmp.write_text("".join(lines))
        tomllib.load(open(tmp, "rb"))
        tmp.replace(cfg_file)
        CONFIG.clear()
        CONFIG.update(tomllib.load(open(cfg_file, "rb")))
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
    state = load_state()
    entry = state["services"].get(name)
    if entry and pid_alive(entry["pid"]) and not force:
        return {"ok": True, "msg": "ya estaba corriendo", "pid": entry["pid"]}
    others = running_of_other_space(state, prof)
    if others:
        if not force:
            raise HTTPException(409, "un solo espacio de trabajo a la vez: "
                                f"corren servicios de otro espacio ({', '.join(others)}). "
                                "Bájalos antes (o usa force=true para cambiar de espacio)")
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
            "worktrees": wts}


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
        st.get("prep", {}).pop(name, None)
    return {"ok": True}


# --- perfiles worktree -------------------------------------------------------
# Fuente de verdad: el perfil (profiles/*.toml, worktree = true) + lo que hay en
# disco (worktrees/{perfil}__{repo}). state.json solo guarda el progreso de la
# preparacion en state["prep"][perfil] = {status: running|ok|error, step, error}.


def wt_profiles() -> dict[str, dict]:
    """Perfiles worktree con algun worktree en disco (o preparandose)."""
    prep = load_state().get("prep", {})
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
        pr = prep.get(f.stem, {})
        if not on_disk and pr.get("status") not in ("running", "error"):
            continue
        out[f.stem] = {"repos": on_disk, "services": profile_services(prof),
                       "status": pr.get("status", "ok"), "step": pr.get("step"),
                       "error": pr.get("error"), "ready": pr.get("status", "ok") == "ok"}
    return out


def wt_pending(profile: str) -> list[str]:
    """Repos del perfil cuyo worktree falta o esta en otra rama: hay que sincronizar."""
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


def _set_prep(profile: str, **info) -> None:
    with state_tx() as st:
        st.setdefault("prep", {})[profile] = info


def wt_sync(profile: str) -> None:
    """Crea o actualiza los worktrees del perfil e instala dependencias. Corre
    en segundo plano: progreso en state["prep"], salida en logs/{perfil}--prep.log.
    Un repo que falla no para al resto; el perfil acaba en error si alguno falla."""
    prof = load_profile(profile) or {}
    bases = prof.get("bases", {})
    LOGS_DIR.mkdir(exist_ok=True)
    log = log_file_for(f"{profile}--prep")
    log.write_text(f"=== preparar {profile} {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")

    def note(msg: str) -> None:
        with open(log, "a") as fh:
            fh.write(f"\n--- {time.strftime('%H:%M:%S')} {msg}\n")

    def run(cmd: list[str], cwd: Path, timeout: int) -> int:
        with open(log, "ab") as fh:
            return subprocess.run([shutil.which(cmd[0]) or cmd[0], *cmd[1:]], cwd=cwd,
                                  stdout=fh, stderr=subprocess.STDOUT, timeout=timeout).returncode

    errors = []
    for rname, branch in prof.get("repos", {}).items():
        try:
            _set_prep(profile, status="running", step=f"{rname}: worktree en {branch}")
            note(f"{rname}: worktree en {branch}")
            d = wt_ensure(rname, branch, profile, bases.get(rname))
            kind = next((r["kind"] for r in repos() if r["name"] == rname), "python")
            cmd = ["npm", "install", "--no-audit", "--no-fund"] if kind == "npm" else ["uv", "sync"]
            _set_prep(profile, status="running", step=f"{rname}: {' '.join(cmd[:2])}")
            note(f"{rname}: {' '.join(cmd)}")
            rc = run(cmd, d, 1800 if kind == "npm" else 900)
            if rc != 0 and kind != "npm":
                # registry de paquetes caido: reintentar con la cache local (wheels de otros repos)
                note(f"{rname}: uv sync --offline")
                rc = run(["uv", "sync", "--offline"], d, 900)
            if rc != 0:
                raise RuntimeError(f"instalacion de dependencias fallida (codigo {rc})")
        except (HTTPException, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            msg = str(e.detail) if isinstance(e, HTTPException) else str(e)
            note(f"ERROR {rname}: {msg}")
            errors.append(f"{rname}: {msg}")
    note("terminado con errores" if errors else "listo")
    if errors:
        _set_prep(profile, status="error", error="; ".join(errors))
    else:
        _set_prep(profile, status="ok")


def wt_start_sync(profile: str) -> None:
    with state_tx() as st:
        if st.get("prep", {}).get(profile, {}).get("status") == "running":
            raise HTTPException(409, f"{profile}: ya se esta preparando")
        st.setdefault("prep", {})[profile] = {"status": "running", "step": "empezando"}
    threading.Thread(target=wt_sync, args=(profile,), daemon=True).start()


def reset_interrupted_prep() -> None:
    """Una preparacion 'running' al arrancar el servidor murio con el anterior."""
    with state_tx() as st:
        for info in st.get("prep", {}).values():
            if info.get("status") == "running":
                info.update(status="error", step=None,
                            error="interrumpida al reiniciar devws; pulsa reintentar")


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
        req = ProfileIn(name=prof, repos={rname: branch}, worktree=True,
                        services=[s["name"] for s in services() if s["repo"] == rname])
    else:
        # se suma al perfil worktree existente (varios repos del mismo perfil)
        req = profile_in(prof, old)
        req.repos = {**req.repos, rname: branch}
    PROFILES_DIR.mkdir(exist_ok=True)
    profile_file(prof).write_text(profile_body(req, old.get("created_at") if old else None))
    wt_start_sync(prof)
    return {"ok": True, "profile": prof,
            "msg": f"{rname} adoptado en el perfil {prof}; comprobando deps en segundo plano"}


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


@app.post("/api/profiles/{name}/apply")
def api_profiles_apply(name: str):
    prof = load_profile(name)
    if prof is None:
        raise HTTPException(404, f"perfil no existe: {name}")
    for r in prof.get("repos", {}):
        repo_path(r)
    if prof.get("worktree"):
        # crear / sincronizar: ramas cambiadas, repos nuevos, deps; en segundo plano
        wt_start_sync(name)
        return {"checkout": [], "services": [], "worktree": True,
                "msg": "preparando worktrees en segundo plano (ver log de preparacion)"}
    state = load_state()
    other = state.get("active_profile")
    running = [n for n, e in state["services"].items() if e and pid_alive(e["pid"])]
    if other and other != name and running:
        raise HTTPException(409, f"perfil activo: {other} (corriendo: {', '.join(running)}). "
                            f"Baja los servicios antes de cargar otro perfil")
    if other == name and running:
        # ya es el perfil activo: idempotente, no relanza checkouts/servicios
        return {"checkout": [], "services": [], "worktree": False,
                "msg": f"perfil {name} ya activo (corriendo: {', '.join(running)})"}
    bases = prof.get("bases", {})
    results = api_checkout(CheckoutReq(items=[CheckoutItem(repo=r, branch=b, base=bases.get(r))
                                              for r, b in prof.get("repos", {}).items()]))
    # orden de arranque = orden del perfil (el formulario lo promete asi)
    ups = [(s, api_up(s, force=True)) for s in profile_services(prof)]
    with state_tx() as st:
        st["active_profile"] = name
    return {"checkout": results, "services": ups, "worktree": False}


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
    if w["status"] == "running":
        raise HTTPException(409, f"{name}: se esta preparando; espera a que termine")
    dirs = [(r, wt_dir_for(name, r)) for r in w["repos"]]
    if any(wt_dirty(d) for _, d in dirs) and not force:
        wt_remove_dirs(dirs, force=False)  # lanza el 409 con el detalle
    wt_down_profile(name)
    errs = wt_remove_dirs(dirs, force=True)
    with state_tx() as st:
        st.get("prep", {}).pop(name, None)
    return {"ok": True, "errors": errs}


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


reset_interrupted_prep()
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


if __name__ == "__main__":
    import uvicorn
    print(f"devws en http://127.0.0.1:{WEB_PORT}")
    uvicorn.run(app, host="127.0.0.1", port=WEB_PORT, log_level="warning")
