"""Monta un workspace de ejemplo para probar devws a mano.

Crea, junto a .devws (en WORKSPACE_ROOT), una cadena de servicios de juguete
    demo-web (npm :4200) -> demo-gw (:8001) -> demo-api (:8000) -> demo-db (:5432)
mas una libreria (demo-lib), cada uno un repo git con remoto "origin" (bare, en
_demo_remotes/) y ramas main / develop / feat-demo que cambian lo que responde
el servicio. La pagina de demo-web ensena de que rama y carpeta (repo original
o worktree) sale cada eslabon.

Uso (desde .devws):  uv run python tests/demo_workspace.py
  --config   escribe tambien config.toml y dos perfiles de ejemplo
             (guarda el config.toml anterior como config.toml.bak)
  --reset    borra antes los repos de demo, sus remotos y los worktrees demo
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

DEVWS = Path(__file__).resolve().parent.parent
ROOT = DEVWS.parent
REMOTES = ROOT / "_demo_remotes"

PY_APP = '''"""Servicio de juguete: responde quien es, de que rama y carpeta corre,
y lo que le contesta el siguiente servicio de la cadena (DOWNSTREAM_URL)."""
import json
import os
import subprocess
import urllib.request
from pathlib import Path


def info() -> dict:
    branch = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
    out = {"service": os.environ.get("SERVICE_NAME", "?"), "branch": branch,
           "message": Path("message.txt").read_text(encoding="utf-8").strip(), "cwd": os.getcwd()}
    url = os.environ.get("DOWNSTREAM_URL")
    if url:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                out["downstream"] = json.loads(r.read())
        except Exception as e:
            out["downstream"] = {"error": f"{url}: {e}"}
    return out


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    body = json.dumps(info(), ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json; charset=utf-8")]})
    await send({"type": "http.response.body", "body": body})
'''

NODE_APP = r'''// Front de juguete: pinta la cadena web -> gw -> api -> db con rama y carpeta.
const http = require("http");
const fs = require("fs");
const { execSync } = require("child_process");
const port = Number(process.argv[process.argv.indexOf("--port") + 1] || 4200);
const GW = process.env.GW_URL || "http://127.0.0.1:8001/";
const esc = s => String(s ?? "").replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const branch = () => { try { return execSync("git branch --show-current").toString().trim(); } catch { return "?"; } };

function card(x) {
  if (!x) return "";
  if (x.error) return `<div class="c err">✕ ${esc(x.error)}</div>`;
  const wt = x.cwd.includes("worktrees") ? '<span class="wt">worktree</span>' : '<span class="og">original</span>';
  return `<div class="c"><b>${esc(x.service)}</b> ${wt}<br>rama <code>${esc(x.branch)}</code> · ${esc(x.message)}
<div class="cwd">${esc(x.cwd)}</div></div>` + card(x.downstream);
}

http.createServer(async (req, res) => {
  let chain;
  try { chain = await (await fetch(GW)).json(); } catch (e) { chain = { error: `${GW}: ${e.message}` }; }
  const me = { service: "demo-web", branch: branch(), message: fs.readFileSync("message.txt", "utf8").trim(),
               cwd: process.cwd(), downstream: chain };
  res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
  res.end(`<!doctype html><meta charset="utf-8"><title>demo devws</title>
<style>body{font:14px system-ui;background:#0d1117;color:#e6edf3;max-width:760px;margin:30px auto}
.c{border:1px solid #30363d;border-radius:8px;padding:10px 14px;margin:0 0 10px 0;background:#161b22}
.c+.c{margin-left:24px}.err{color:#f85149}.cwd{color:#8b949e;font-size:12px}
.wt{color:#d2a8ff}.og{color:#3fb950}code{color:#58a6ff}</style>
<h2>Cadena de servicios de demo</h2>${card(me)}<p class="cwd">recarga la pagina tras cambiar de perfil</p>`);
}).listen(port, "127.0.0.1", () => console.log(`demo-web en http://127.0.0.1:${port}`));
'''

GITIGNORE = ".env\n.venv/\nnode_modules/\n__pycache__/\n"

# nombre, path relativo a ROOT, tipo, puerto, siguiente eslabon, mensajes por rama
REPOS = [
    ("demo-db", "commons/demo-db", "python", 5432, None,
     {"main": "base de datos v1", "develop": "base de datos v2 (migracion nueva)", "feat-demo": "db con tabla extra"}),
    ("demo-api", "demo-api", "python", 8000, 5432,
     {"main": "api estable", "develop": "api develop", "feat-demo": "api con endpoint nuevo ✨"}),
    ("demo-gw", "demo-gw", "python", 8001, 8000,
     {"main": "gateway estable", "develop": "gateway develop", "feat-demo": "gateway con cache"}),
    ("demo-web", "demo-web", "npm", 4200, 8001,
     {"main": "web estable", "develop": "web develop (nuevo diseño)", "feat-demo": "web con boton nuevo"}),
    ("demo-lib", "demo-lib", "python", None, None,
     {"main": "lib 1.0", "develop": "lib 1.1-dev", "feat-demo": "lib con helper nuevo"}),
]


def run(cwd: Path, *args: str) -> str:
    exe = shutil.which(args[0]) or args[0]
    res = subprocess.run([exe, *args[1:]], cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"fallo en {cwd}: {' '.join(args)}\n{res.stderr}")
    return res.stdout


def commit_all(d: Path, msg: str) -> None:
    run(d, "git", "add", "-A")
    run(d, "git", "commit", "-q", "-m", msg)


def make_repo(name: str, rel: str, kind: str, port, down, messages: dict) -> None:
    d = ROOT / rel
    if d.exists():
        sys.exit(f"{d} ya existe (usa --reset para rehacer la demo)")
    remote = REMOTES / f"{name}.git"
    REMOTES.mkdir(exist_ok=True)
    run(ROOT, "git", "init", "-q", "--bare", "-b", "main", str(remote))
    d.mkdir(parents=True)
    run(d, "git", "init", "-q", "-b", "main")
    (d / ".gitignore").write_text(GITIGNORE, encoding="utf-8", newline="\n")
    (d / "message.txt").write_text(messages["main"] + "\n", encoding="utf-8", newline="\n")
    if kind == "npm":
        pkg = {"name": name, "version": "0.0.0", "private": True, "scripts": {"start": "node server.js"}}
        (d / "package.json").write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8", newline="\n")
        (d / "server.js").write_text(NODE_APP, encoding="utf-8", newline="\n")
        run(d, "npm", "install", "--package-lock-only", "--no-audit", "--no-fund")
    else:
        deps = '"uvicorn>=0.30"' if port else ""
        (d / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n'
            f'dependencies = [{deps}]\n\n[tool.uv]\npackage = false\n', encoding="utf-8", newline="\n")
        if port:
            (d / "app.py").write_text(PY_APP, encoding="utf-8", newline="\n")
        run(d, "uv", "lock", "-q")
    commit_all(d, "init")
    run(d, "git", "remote", "add", "origin", str(remote))
    run(d, "git", "push", "-q", "-u", "origin", "main")
    for b in ("develop", "feat-demo"):
        run(d, "git", "checkout", "-q", "-b", b, "main" if b == "develop" else "develop")
        (d / "message.txt").write_text(messages[b] + "\n", encoding="utf-8", newline="\n")
        commit_all(d, f"{b}: {messages[b]}")
        run(d, "git", "push", "-q", "-u", "origin", b)
    run(d, "git", "checkout", "-q", "main")
    run(d, "git", "branch", "-q", "-D", "feat-demo")  # solo en origin: devws la trae al usarla
    run(d, "git", "remote", "set-head", "origin", "main")
    # .env ignorado por git, como en los repos reales
    env = {"SERVICE_NAME": name}
    if down:
        env["GW_URL" if kind == "npm" else "DOWNSTREAM_URL"] = f"http://127.0.0.1:{down}/"
    (d / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8", newline="\n")
    # dependencias del repo original (servicios in-place)
    if kind == "npm":
        run(d, "npm", "install", "--no-audit", "--no-fund")
    elif port:
        run(d, "uv", "sync", "-q")
    print(f"  {name:9} {rel:18} {kind:6} {':' + str(port) if port else '(libreria)'}")


def write_config() -> None:
    py = ".venv/Scripts/python.exe" if sys.platform == "win32" else "./.venv/bin/python"
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    lines = ["# config de la demo (tests/demo_workspace.py --config)", "", "[meta]", "web_port = 5000", ""]
    for name, rel, kind, port, down, _ in REPOS:
        lines += ["[[repos]]", f'name = "{name}"', f'path = "{rel}"', f'kind = "{kind}"']
        if name == "demo-api":
            lines.append('copy = [".env"]')
        lines.append("")
    lines.append("# orden de arranque = orden de aparicion")
    for name, rel, kind, port, down, _ in REPOS:
        if not port:
            continue
        cmd = ([npm, "run", "start", "--", "--port", "{port}"] if kind == "npm" else
               [py, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "{port}", "--reload"])
        lines += ["[[services]]", f'name = "{name.removeprefix("demo-")}"', f'repo = "{name}"',
                  f"port = {port}", "cmd = " + json.dumps(cmd), ""]
    lines += ["[[libraries]]", 'name = "demo-lib"', 'consumers = ["demo-api", "demo-gw"]', ""]
    cfg = DEVWS / "config.toml"
    if cfg.exists():
        shutil.copy2(cfg, cfg.with_suffix(".toml.bak"))
    cfg.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    profiles = DEVWS / "profiles"
    profiles.mkdir(exist_ok=True)
    all_svcs = '["db", "api", "gw", "web"]'
    (profiles / "estable.toml").write_text(
        'name = "estable"\ncreated_at = "demo"\nworktree = false\n\n[repos]\n'
        + "".join(f'"{n}" = "main"\n' for n, *_ in REPOS)
        + f"\n[services]\nservices = {all_svcs}\n", encoding="utf-8", newline="\n")
    (profiles / "feature-api.toml").write_text(
        'name = "feature-api"\ncreated_at = "demo"\nworktree = true\n\n[repos]\n'
        '"demo-api" = "feat-demo"\n"demo-web" = "develop"\n'
        f"\n[services]\nservices = {all_svcs}\n", encoding="utf-8", newline="\n")
    print(f"  config.toml escrito (anterior en config.toml.bak) y perfiles: estable, feature-api")


def reset() -> None:
    def force_rm(path: Path) -> None:
        if path.exists():
            shutil.rmtree(path, onexc=lambda f, p, e: (Path(p).chmod(0o700), f(p)))
    for d in (DEVWS / "worktrees").glob("*__demo-*") if (DEVWS / "worktrees").exists() else []:
        force_rm(d)
    for _, rel, *_ in REPOS:
        force_rm(ROOT / rel)
    force_rm(REMOTES)
    commons = ROOT / "commons"
    if commons.exists() and not any(commons.iterdir()):
        commons.rmdir()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", action="store_true", help="escribe config.toml y perfiles de ejemplo")
    ap.add_argument("--reset", action="store_true", help="borra la demo anterior antes de crearla")
    args = ap.parse_args()
    if args.reset:
        reset()
    print(f"creando repos de demo en {ROOT}")
    for r in REPOS:
        make_repo(*r)
    if args.config:
        write_config()
    print("listo: arranca devws (python server.py) y abre http://127.0.0.1:5000")


if __name__ == "__main__":
    main()
