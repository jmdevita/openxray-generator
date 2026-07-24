"""orchestrator: the control plane: pipeline jobs + web dashboard (plan U2b).

Runs the SAME pipeline code as `xray run` (xray/pipeline.py); this service
adds a job queue (one worker: the passes are already concurrency-shy),
per-job logs, an optional nightly schedule, and the dashboard that makes the
container self-sufficient: setup wizard (Plex PIN / Jellyfin Quick Connect),
web-managed settings, share actions (export / hub upload / import), and a
web-token auth gate on every route (SECURITY.md).

  GET  /                 dashboard (setup, settings, store, jobs, actions)
  GET  /login            web-token login (token printed to container logs)
  POST /api/login        {"token"} → session cookie
  GET  /api/status       store inventory as JSON
  POST /api/run          {"rating_key"|"search"|"library", "max_titles",
                          "skip", "level"}
  GET  /api/jobs         job list (+ ?id= for one job with full log)
  …/api/setup, /api/settings, /api/auth/plex/*, /api/auth/jellyfin/*,
  /api/export/{cid}, /api/hub/upload/{cid}, /api/import, /api/validate

Config: settings.json in the store volume (web-managed; env vars seed it on
first boot; see xray/settings_store.py). AUTH_METHOD=external delegates the
gate to a reverse proxy. XRAY_AUDD_BUDGET, SCHEDULE_* stay env-only.
"""
from __future__ import annotations

import json
import os
import secrets as pysecrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .. import keys as k
from .. import pipeline, settings_store as ss, store as st
from ..budget import AuddBudget
from . import media_auth as ma

STORE = Path(os.environ.get("XRAY_STORE",
                            str(Path.home() / ".plex-xray" / "timelines")))
AUDD_BUDGET = int(os.environ.get("XRAY_AUDD_BUDGET", "300"))
AUTH_EXTERNAL = os.environ.get("AUTH_METHOD", "").strip().lower() == "external"


def _web_token() -> str:
    """The dashboard credential. XRAY_WEB_TOKEN env overrides ALWAYS (declarative
    compose setups + instant rotation: the docker idiom for credentials);
    otherwise the first-boot generated value in settings.json."""
    return os.environ.get("XRAY_WEB_TOKEN", "").strip() or ss.get("web_token")


def _backend() -> str:
    return ss.get("backend") or os.environ.get("XRAY_BACKEND", "") or "plex"


def _origin() -> str:
    if _backend() == "jellyfin":
        return ss.get("jellyfin_origin") or os.environ.get("JELLYFIN_ORIGIN", "")
    return ss.get("plex_origin") or os.environ.get("PLEX_ORIGIN", "")


def _client_id() -> str:
    cid = ss.get("client_id")
    if not cid:
        import uuid
        cid = str(uuid.uuid4())
        ss.update({"client_id": cid})
    return cid


def _source():
    from ..sources.base import open_source
    backend = _backend()
    return open_source(backend, _origin(), k.backend_token(backend),
                       user_id=k.jellyfin_user() or None)

app = FastAPI(title="OpenXray orchestrator", version="0.1.0")

_jobs: list[dict] = []          # newest first; [{id,target,status,log,...}]
_queue: list[dict] = []
_lock = threading.Lock()


# --- auth gate --------------------------------------------------------------

_COOKIE = "xray_auth"
_OPEN_PATHS = {"/login", "/api/login", "/health"}


def _supplied_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-auth-token")
            or request.cookies.get(_COOKIE) or "").strip()


@app.middleware("http")
async def _gate(request: Request, call_next):
    if AUTH_EXTERNAL or request.url.path in _OPEN_PATHS:
        return await call_next(request)
    expected = _web_token()
    if expected and pysecrets.compare_digest(_supplied_token(request), expected):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "auth required"}, status_code=401)
    return RedirectResponse("/login")


class LoginRequest(BaseModel):
    token: str


@app.post("/api/login")
def api_login(req: LoginRequest):
    expected = _web_token()
    if not expected or not pysecrets.compare_digest(req.token.strip(), expected):
        raise HTTPException(401, "wrong token; see the container logs")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(_COOKIE, expected, httponly=True, samesite="lax",
                    max_age=90 * 24 * 3600)
    return resp


@app.get("/health")
def health():
    return {"ok": True}


# --- jobs -------------------------------------------------------------------

class RunRequest(BaseModel):
    rating_key: str | None = None
    search: str | None = None
    library: str | None = None
    max_titles: int = 0
    skip: str = ""
    level: int = 1  # 0 = video-free seed, 1 = full index


def _submit(req: RunRequest) -> dict:
    with _lock:
        job = {"id": len(_jobs) + 1,
               "target": req.search or req.library or req.rating_key,
               "request": req.model_dump(), "status": "queued",
               "log": [], "summary": [], "created": _now()}
        _jobs.insert(0, job)
        _queue.append(job)
    return job


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _worker() -> None:
    while True:
        with _lock:
            job = _queue.pop(0) if _queue else None
        if job is None:
            time.sleep(2)
            continue
        job["status"] = "running"
        log = job["log"].append
        try:
            req = RunRequest(**job["request"])
            source = _source()
            targets = pipeline.enumerate_targets(
                source, rating_key=req.rating_key,
                search=req.search, library=req.library,
                max_titles=req.max_titles)
            log(f"{len(targets)} target(s)")
            skip = set(req.skip.split(",")) - {""}
            for rk in targets:
                job["summary"].append(pipeline.run_title(
                    STORE, source=source,
                    tmdb_key=k.tmdb_key(), audd_token=k.audd_token(),
                    rating_key=rk, skip=skip, audd_budget=AUDD_BUDGET,
                    hub_url=k.hub_url(), hub_miss="index",  # services never prompt
                    level=req.level, log=log))
            job["status"] = "done"
        except Exception as e:  # noqa: BLE001
            log(f"JOB FAILED: {e}")
            job["status"] = "failed"


def _scheduler() -> None:
    lib = os.environ.get("SCHEDULE_LIBRARY", "")
    if not lib:
        return
    hour = int(os.environ.get("SCHEDULE_HOUR_UTC", "3"))
    n = int(os.environ.get("SCHEDULE_MAX_TITLES", "3"))
    last_day = ""
    while True:
        now = datetime.now(timezone.utc)
        if now.hour == hour and now.strftime("%F") != last_day:
            last_day = now.strftime("%F")
            _submit(RunRequest(library=lib, max_titles=n))
        time.sleep(300)


@app.on_event("startup")
def _start_threads():
    if ss.ensure_seeded():
        print("=" * 62)
        print("  first boot: settings.json created in the store volume")
        print("=" * 62)
    if not AUTH_EXTERNAL:
        if os.environ.get("XRAY_WEB_TOKEN", "").strip():
            print("  web UI token: set via XRAY_WEB_TOKEN (env override)")
        else:
            # mpg-style: the way in is always in the logs (SECURITY.md).
            print(f"  web UI token: {ss.get('web_token')}")
            print("  open the dashboard and paste it once; sent as a cookie after.")
    threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_scheduler, daemon=True).start()


# --- store / jobs API -------------------------------------------------------

def _inventory() -> list[dict]:
    out = []
    manifest = st.load_manifest(STORE)
    rev: dict[str, list[str]] = {}
    for lk, fn in manifest.get("lookup", {}).items():
        rev.setdefault(fn, []).append(lk)
    for f in st.timeline_files(STORE):
        doc = json.loads(f.read_text())
        prov = doc.get("provenance") or {}
        out.append({
            "contentId": doc.get("contentId") or f.stem,
            "blocks": {b: (prov.get(b) or {}).get("generated")
                       for b in ("faces", "people", "music", "trivia")},
            "lookup": rev.get(f.name, []),
            "intervals": len(doc.get("actorIntervals") or []),
            "songs": len(doc.get("musicIntervals") or []),
            "trivia": len(doc.get("trivia") or []),
        })
    return out


@app.get("/api/status")
def api_status():
    b = AuddBudget(STORE, AUDD_BUDGET)
    return {"store": str(STORE), "origin": _origin(), "backend": _backend(),
            "auddUsed": b.used, "auddMonthly": b.monthly,
            "titles": _inventory()}


@app.post("/api/run")
def api_run(req: RunRequest):
    if not (req.rating_key or req.search or req.library):
        raise HTTPException(422, "need rating_key, search, or library")
    if not _origin():
        raise HTTPException(503, "no media server configured; run Setup")
    job = _submit(req)
    return {"id": job["id"], "status": job["status"]}


@app.get("/api/search")
def api_search(q: str):
    """Library candidates for a query: the UI shows these and queues the
    user's PICK by ratingKey (no more blind first-match)."""
    if not _origin():
        raise HTTPException(503, "no media server configured; run Setup")
    out = []
    for r in _source().search(q):
        if r.get("type") not in ("movie", "episode"):
            continue
        label = r.get("title") or ""
        if r.get("type") == "episode" and r.get("grandparentTitle"):
            label = (f"{r['grandparentTitle']} "
                     f"S{int(r.get('season') or 0):02d}"
                     f"E{int(r.get('episode') or 0):02d} · {label}")
        out.append({"ratingKey": r.get("ratingKey"), "type": r.get("type"),
                    "label": label, "year": r.get("year")})
    return {"results": out}


@app.get("/api/jobs")
def api_jobs(id: int | None = None):
    if id is not None:
        for j in _jobs:
            if j["id"] == id:
                return j
        raise HTTPException(404, "no such job")
    return [{kk: j[kk] for kk in ("id", "target", "status", "created")}
            for j in _jobs[:50]]


# --- setup wizard -----------------------------------------------------------

@app.get("/api/setup")
def api_setup():
    return {
        "backend": _backend(),
        "plex": {"origin": ss.get("plex_origin"),
                 "signedIn": bool(ss.get("plex_token"))},
        "jellyfin": {"origin": ss.get("jellyfin_origin"),
                     "signedIn": bool(ss.get("jellyfin_token"))},
        "tmdbConfigured": bool(k.tmdb_key()),
        "auddConfigured": bool(k.audd_token()),
        "hubUrl": k.hub_url(),
    }


@app.post("/api/auth/plex/pin")
def plex_pin():
    return ma.plex_create_pin(_client_id())


@app.get("/api/auth/plex/pin/{pin_id}")
def plex_pin_poll(pin_id: int):
    token = ma.plex_check_pin(_client_id(), pin_id)
    if token:
        ss.update({"plex_token": token, "backend": _backend()})
    return {"claimed": bool(token)}


@app.get("/api/auth/plex/servers")
def plex_server_list():
    token = ss.get("plex_token")
    if not token:
        raise HTTPException(409, "sign in with Plex first")
    return {"servers": ma.plex_servers(_client_id(), token)}


class OriginRequest(BaseModel):
    uri: str


@app.post("/api/auth/plex/origin")
def plex_set_origin(req: OriginRequest):
    uri = req.uri.rstrip("/")
    ss.update({"plex_origin": uri, "backend": "plex"})
    # Reachability probe FROM THIS CONTAINER: the vantage point that matters.
    # A "local" LAN connection can be dead from here (server actually remote,
    # VPN topology, etc.) while other picks work; warn immediately instead of
    # letting the first job fail with a connection error.
    reachable = True
    try:
        import requests
        requests.get(f"{uri}/identity", timeout=5).raise_for_status()
    except Exception:  # noqa: BLE001 (any failure = unreachable)
        reachable = False
    return {"ok": True, "origin": uri, "reachable": reachable}


class JfConnectRequest(BaseModel):
    origin: str


@app.post("/api/auth/jellyfin/quickconnect")
def jf_quickconnect(req: JfConnectRequest):
    origin = req.origin.rstrip("/")
    ss.update({"jellyfin_origin": origin})
    if not ma.jf_quickconnect_enabled(origin, _client_id()):
        return {"enabled": False}
    out = ma.jf_quickconnect_initiate(origin, _client_id())
    return {"enabled": True, **out}


@app.get("/api/auth/jellyfin/quickconnect/{secret}")
def jf_quickconnect_poll(secret: str):
    origin = ss.get("jellyfin_origin")
    if not origin:
        raise HTTPException(409, "no Jellyfin origin; start over")
    if not ma.jf_quickconnect_claimed(origin, _client_id(), secret):
        return {"claimed": False}
    got = ma.jf_quickconnect_exchange(origin, _client_id(), secret)
    ss.update({"jellyfin_token": got["token"], "jellyfin_user": got["user_id"],
               "backend": "jellyfin"})
    return {"claimed": True}


class JfPasswordRequest(BaseModel):
    origin: str
    username: str
    password: str


@app.post("/api/auth/jellyfin/password")
def jf_password(req: JfPasswordRequest):
    origin = req.origin.rstrip("/")
    got = ma.jf_password_auth(origin, _client_id(), req.username, req.password)
    ss.update({"jellyfin_origin": origin, "jellyfin_token": got["token"],
               "jellyfin_user": got["user_id"], "backend": "jellyfin"})
    return {"ok": True}


# --- settings ---------------------------------------------------------------

@app.get("/api/settings")
def api_settings():
    return ss.redacted()


class SettingsPatch(BaseModel):
    backend: str | None = None
    tmdb_key: str | None = None
    audd_token: str | None = None
    hub_url: str | None = None


@app.put("/api/settings")
def api_settings_put(patch: SettingsPatch):
    if patch.backend and patch.backend not in ("plex", "jellyfin"):
        raise HTTPException(422, "backend must be plex or jellyfin")
    ss.update(patch.model_dump())
    return ss.redacted()


# --- share actions ----------------------------------------------------------

@app.get("/api/export/{content_id}")
def api_export(content_id: str):
    from ..share import export_timeline
    out_dir = STORE / "exports"
    try:
        out = export_timeline(STORE, content_id, out_dir)
    except SystemExit as e:
        raise HTTPException(422, str(e))
    return FileResponse(out, media_type="application/json", filename=out.name)


@app.post("/api/hub/upload/{content_id}")
def api_hub_upload(content_id: str):
    from ..share import upload_to_hub
    hub = k.hub_url()
    if not hub:
        raise HTTPException(503, "no hub URL configured (Settings)")
    try:
        return upload_to_hub(STORE, content_id, hub)
    except SystemExit as e:
        raise HTTPException(422, str(e))


class ImportRequest(BaseModel):
    src: str  # URL (or container-visible path)


@app.post("/api/import")
def api_import(req: ImportRequest):
    from ..share import import_timeline
    try:
        dest = import_timeline(STORE, req.src)
    except SystemExit as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "file": dest.name}


@app.get("/api/validate")
def api_validate():
    results = []
    for f in st.timeline_files(STORE):
        try:
            st.validate(json.loads(f.read_text()))
            results.append({"file": f.name, "valid": True})
        except Exception as e:  # noqa: BLE001
            results.append({"file": f.name, "valid": False,
                            "error": str(e).splitlines()[0]})
    return {"results": results}


# --- pages ------------------------------------------------------------------

_STYLE = """<style>
 body{font:14px/1.5 system-ui;margin:2rem auto;max-width:980px;color:#1d1e22;padding:0 1rem}
 h1{font-size:20px} h2{font-size:15px;margin-top:1.6em}
 table{border-collapse:collapse;width:100%;font-size:13px}
 th,td{text-align:left;padding:5px 9px;border-bottom:1px solid #ddd;font-variant-numeric:tabular-nums}
 th{color:#666;font-weight:600}
 .ok{color:#2e7d32}.miss{color:#bbb}.warn{color:#b26a00}
 form,.row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0;align-items:center}
 input,select,button{font:inherit;padding:5px 9px;border:1px solid #bbb;border-radius:5px}
 button{background:#1d1e22;color:#fff;border-color:#1d1e22;cursor:pointer}
 button.ghost{background:none;color:inherit;border-color:#bbb;padding:2px 8px;font-size:12px}
 pre{background:#f4f4f2;padding:10px;border-radius:6px;font-size:12px;overflow-x:auto;max-height:300px}
 .meta{color:#666;font-size:12.5px} .card{border:1px solid #ddd;border-radius:8px;padding:10px 14px;margin:8px 0}
 .code{font-size:26px;letter-spacing:6px;font-weight:700}
 @media(prefers-color-scheme:dark){body{background:#17181c;color:#e9e9ec}
  th,td,.card{border-color:#333}th,.meta{color:#9a9ba3}pre{background:#1f2026}
  input,select{background:#1f2026;color:#e9e9ec;border-color:#444}
  button.ghost{border-color:#444}}
</style>"""

_LOGIN_PAGE = f"""<!doctype html><meta charset="utf-8"><title>xray sign-in</title>
{_STYLE}
<h1>xray orchestrator</h1>
<div class="card"><p>Paste the <b>web UI token</b> from the container logs
(<code>docker compose logs orchestrator</code>).</p>
<form onsubmit="login(this);return false">
 <input name="token" size="44" placeholder="web UI token" autofocus>
 <button>Sign in</button> <span id="err" class="warn"></span>
</form></div>
<script>
async function login(f){{
 const r = await fetch('/api/login', {{method:'POST',
   headers:{{'content-type':'application/json'}},
   body: JSON.stringify({{token: f.token.value.trim()}})}});
 if(r.ok) location = '/';
 else document.getElementById('err').textContent = (await r.json()).detail;
 return false;
}}
</script>"""

_DASH_PAGE = f"""<!doctype html><meta charset="utf-8"><title>xray orchestrator</title>
{_STYLE}
<h1>xray orchestrator</h1>
<div class="meta" id="meta"></div>

<h2>Setup</h2>
<div class="card" id="setup">loading…</div>

<h2>Settings</h2>
<div class="card"><form onsubmit="saveSettings(this);return false" id="settingsForm">
 <select name="backend"><option value="plex">plex</option><option value="jellyfin">jellyfin</option></select>
 <input name="tmdb_key" placeholder="TMDb key" size="18">
 <input name="audd_token" placeholder="AudD token (music, optional)" size="20">
 <input name="hub_url" placeholder="hub URL (optional)" size="22">
 <button>Save</button> <span id="settingsState" class="meta"></span>
</form></div>

<h2>Run</h2>
<form onsubmit="doSearch(this);return false" id="runForm">
 <input name="search" placeholder="search title…" id="searchBox">
 <button>Search</button>
 <span class="meta">or direct:</span>
 <input name="rating_key" placeholder="ratingKey" size="9">
 <input name="library" placeholder="library name" size="12">
 <input name="max_titles" placeholder="max" size="4" value="3">
 <input name="skip" placeholder="skip (e.g. music)" size="12">
 <label><input type="checkbox" name="seed"> level-0 seed (no video, fast)</label>
 <button type="button" onclick="runDirect()">Queue</button>
</form>
<div id="searchResults"></div>

<h2>Store</h2><table id="titles"></table>
<div class="row"><input id="importSrc" placeholder="import URL (hub or shared file)" size="40">
<button class="ghost" onclick="doImport()">Import</button>
<button class="ghost" onclick="doValidate()">Validate all</button></div>
<pre id="out" hidden></pre>

<h2>Jobs</h2><table id="jobs"></table><pre id="joblog" hidden></pre>

<script>
const j = r => r.json();
const post = (url, body) => fetch(url, {{method:'POST',
  headers:{{'content-type':'application/json'}},
  body: body ? JSON.stringify(body) : undefined}});

async function refresh(){{
 const s = await j(await fetch('api/status'));
 document.getElementById('meta').textContent =
  `store ${{s.store}} · ${{s.backend}} ${{s.origin||'(unset)'}} · AudD ${{s.auddUsed}}/${{s.auddMonthly||'∞'}} this month`;
 const tt = document.getElementById('titles');
 tt.innerHTML = '<tr><th>title</th><th>faces</th><th>people</th><th>music</th><th>trivia</th><th>intervals</th><th>songs</th><th></th></tr>';
 for(const t of s.titles){{
  const b = n => t.blocks[n] ? `<span class=ok>✓ ${{t.blocks[n].slice(0,10)}}</span>` : '<span class=miss>none</span>';
  tt.innerHTML += `<tr><td>${{t.contentId}}</td><td>${{b('faces')}}</td><td>${{b('people')}}</td><td>${{b('music')}}</td><td>${{b('trivia')}}</td><td>${{t.intervals}}</td><td>${{t.songs}}</td>
   <td><a class="ghost" href="api/export/${{t.contentId}}">export</a>
   <button class="ghost" onclick="hubUpload('${{t.contentId}}')">→ hub</button></td></tr>`;
 }}
 const jobs = await j(await fetch('api/jobs'));
 const jt = document.getElementById('jobs');
 jt.innerHTML = '<tr><th>id</th><th>target</th><th>status</th><th>created</th></tr>';
 for(const jb of jobs) jt.innerHTML += `<tr onclick="showLog(${{jb.id}})" style="cursor:pointer"><td>${{jb.id}}</td><td>${{jb.target||''}}</td><td>${{jb.status}}</td><td>${{jb.created}}</td></tr>`;
}}

async function loadSetup(){{
 const s = await j(await fetch('api/setup'));
 const el = document.getElementById('setup');
 const plex = s.plex.signedIn
   ? `<span class=ok>✓ Plex signed in</span> <span class=meta>${{s.plex.origin||'(pick a server)'}}</span>
      <button class=ghost onclick="pickServer()">${{s.plex.origin ? 'change server' : 'choose server'}}</button>`
   : `<button onclick="plexSignIn()">Sign in with Plex</button>`;
 const jf = s.jellyfin.signedIn
   ? `<span class=ok>✓ Jellyfin connected</span> <span class=meta>${{s.jellyfin.origin}}</span>`
   : `<input id="jfOrigin" placeholder="http://jellyfin:8096" size="24" value="${{s.jellyfin.origin||''}}">
      <button onclick="jfQuick()">Quick Connect</button>
      <input id="jfUser" placeholder="user" size="9"><input id="jfPass" type="password" placeholder="password" size="10">
      <button class="ghost" onclick="jfPassword()">password sign-in</button>`;
 el.innerHTML = `<div class="row"><b>Plex</b> ${{plex}}</div><div id="plexFlow"></div>
   <div class="row"><b>Jellyfin</b> ${{jf}}</div><div id="jfFlow"></div>
   <div class="row meta">TMDb key ${{s.tmdbConfigured?'<span class=ok>✓</span>':'<span class=warn>required: set below</span>'}} ·
   AudD ${{s.auddConfigured?'<span class=ok>✓</span>':'none'}} · hub ${{s.hubUrl||'not set'}}</div>`;
}}

async function plexSignIn(){{
 const pin = await j(await post('api/auth/plex/pin'));
 window.open(pin.authUrl, '_blank');
 document.getElementById('plexFlow').innerHTML =
  `<div class="card">Finish signing in on the Plex tab… <span class="meta">(code ${{pin.code.slice(0,4)}}…)</span></div>`;
 for(let i=0;i<120;i++){{
  await new Promise(r=>setTimeout(r,3000));
  const st = await j(await fetch('api/auth/plex/pin/'+pin.id));
  if(st.claimed) return pickServer();
 }}
}}

async function pickServer(){{
 const data = await j(await fetch('api/auth/plex/servers'));
 const opts = [];
 for(const srv of data.servers)
  for(const c of srv.connections)
   opts.push(`<option value="${{c.uri}}">${{srv.name}} · ${{c.uri}}${{c.local?' (local)':''}}${{c.relay?' (relay)':''}}</option>`);
 document.getElementById('plexFlow').innerHTML =
  `<div class="card"><select id="srvPick">${{opts.join('')}}</select>
   <button onclick="saveServer()">Use this server</button></div>`;
}}

async function saveServer(){{
 const r = await j(await post('api/auth/plex/origin', {{uri: document.getElementById('srvPick').value}}));
 document.getElementById('plexFlow').innerHTML = r.reachable ? '' :
  `<div class="card warn">Saved, but this address is NOT reachable from the container:
   pick a different connection (a LAN address can be dead from here while the remote one works).</div>`;
 loadSetup(); refresh();
}}

async function jfQuick(){{
 const origin = document.getElementById('jfOrigin').value.trim();
 if(!origin) return;
 const r = await j(await post('api/auth/jellyfin/quickconnect', {{origin}}));
 const flow = document.getElementById('jfFlow');
 if(!r.enabled){{ flow.innerHTML = '<div class="card warn">Quick Connect is disabled on this server; use password sign-in.</div>'; return; }}
 flow.innerHTML = `<div class="card">Enter this code in Jellyfin (Settings → Quick Connect):
   <div class="code">${{r.code}}</div></div>`;
 for(let i=0;i<120;i++){{
  await new Promise(res=>setTimeout(res,3000));
  const st = await j(await fetch('api/auth/jellyfin/quickconnect/'+r.secret));
  if(st.claimed){{ flow.innerHTML=''; loadSetup(); refresh(); return; }}
 }}
}}

async function jfPassword(){{
 const body = {{origin: document.getElementById('jfOrigin').value.trim(),
   username: document.getElementById('jfUser').value,
   password: document.getElementById('jfPass').value}};
 const r = await post('api/auth/jellyfin/password', body);
 if(r.ok){{ loadSetup(); refresh(); }}
 else document.getElementById('jfFlow').innerHTML =
  `<div class="card warn">${{(await r.json()).detail || 'sign-in failed'}}</div>`;
}}

async function saveSettings(f){{
 const body = {{}};
 for(const el of f.elements) if(el.name && el.value) body[el.name] = el.value;
 await fetch('api/settings', {{method:'PUT', headers:{{'content-type':'application/json'}}, body: JSON.stringify(body)}});
 document.getElementById('settingsState').textContent = 'saved';
 f.tmdb_key.value = f.audd_token.value = '';
 loadSetup(); return false;
}}

async function hubUpload(cid){{
 const r = await post('api/hub/upload/'+cid);
 const out = document.getElementById('out'); out.hidden = false;
 out.textContent = JSON.stringify(await r.json(), null, 1);
}}
async function doImport(){{
 const src = document.getElementById('importSrc').value.trim();
 if(!src) return;
 const r = await post('api/import', {{src}});
 const out = document.getElementById('out'); out.hidden = false;
 out.textContent = JSON.stringify(await r.json(), null, 1); refresh();
}}
async function doValidate(){{
 const r = await j(await fetch('api/validate'));
 const out = document.getElementById('out'); out.hidden = false;
 out.textContent = r.results.map(x=>`${{x.file}}: ${{x.valid?'VALID':'INVALID: '+x.error}}`).join('\\n') || '(store is empty)';
}}

function runOptions(){{
 const f = document.getElementById('runForm'), body = {{}};
 body.level = f.seed.checked ? 0 : 1;
 if(f.skip.value) body.skip = f.skip.value;
 if(f.max_titles.value) body.max_titles = +f.max_titles.value;
 return body;
}}

async function doSearch(f){{
 const q = f.search.value.trim();
 if(!q) return;
 const el = document.getElementById('searchResults');
 el.innerHTML = '<div class="meta">searching…</div>';
 const r = await fetch('api/search?q='+encodeURIComponent(q));
 if(!r.ok){{ el.innerHTML = `<div class="card warn">${{(await r.json()).detail}}</div>`; return; }}
 const data = await r.json();
 if(!data.results.length){{ el.innerHTML = '<div class="card meta">no matches in the library</div>'; return; }}
 el.innerHTML = '<div class="card">' + data.results.map(x =>
  `<div class="row">${{x.label}}${{x.year ? ` <span class=meta>(${{x.year}})</span>` : ''}}
   <span class=meta>${{x.type}} · ${{x.ratingKey}}</span>
   <button class="ghost" onclick="queuePick('${{x.ratingKey}}', ${{JSON.stringify(x.label)}})">Queue</button></div>`
 ).join('') + '</div>';
}}

async function queuePick(ratingKey, label){{
 const body = {{...runOptions(), rating_key: ratingKey, search: label}};
 const r = await post('api/run', body);
 if(!r.ok) alert((await r.json()).detail);
 document.getElementById('searchResults').innerHTML = '';
 document.getElementById('searchBox').value = '';
 refresh();
}}

async function runDirect(){{
 const f = document.getElementById('runForm'), body = runOptions();
 if(f.rating_key.value) body.rating_key = f.rating_key.value;
 else if(f.library.value) body.library = f.library.value;
 else return alert('enter a ratingKey or library name (or use Search)');
 const r = await post('api/run', body);
 if(!r.ok) alert((await r.json()).detail);
 refresh();
}}
async function showLog(id){{
 const jb = await j(await fetch('api/jobs?id='+id));
 const el = document.getElementById('joblog');
 el.hidden = false; el.textContent = jb.log.join('\\n') || '(no log yet)';
}}

loadSetup(); refresh(); setInterval(refresh, 5000);
</script>"""


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return _LOGIN_PAGE


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return _DASH_PAGE
