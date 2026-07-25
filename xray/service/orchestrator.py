"""orchestrator: the control plane: pipeline jobs + web dashboard (plan U2b).

Runs the SAME pipeline code as `xray run` (xray/pipeline.py); this service
adds a job queue (one worker: the passes are already concurrency-shy),
per-job logs, an optional nightly schedule, and the dashboard that makes the
container self-sufficient: setup wizard (Plex PIN / Jellyfin Quick Connect),
web-managed settings, share actions (export / hub upload / import), and a
web-token auth gate on every route (SECURITY.md).

  GET  /                 dashboard (setup gate, run composer, jobs, store)
  GET  /login            web-token login (token printed to container logs)
  POST /api/login        {"token"} → session cookie
  GET  /api/status       store inventory as JSON
  POST /api/run          {"rating_key"|"search"|"library", "max_titles",
                          "skip", "level"}
  GET  /api/libraries    library sections to run against
  GET  /api/plan         ?library= → coverage + per-level cost estimate
  GET  /api/jobs         job list (+ ?id= for one job, &log=0 to omit the log)
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


def _upload_token() -> str:
    """Imported lazily so share.py's `requests` stays off the startup path."""
    from ..share import upload_token
    return upload_token()


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
    series: str | None = None   # every episode of one show
    max_titles: int = 0
    skip: str = ""
    level: int = 1  # 0 = video-free seed, 1 = full index


def _submit(req: RunRequest) -> dict:
    with _lock:
        job = {"id": len(_jobs) + 1,
               "target": (req.search or req.library or req.series
                          or req.rating_key),
               "request": req.model_dump(), "status": "queued",
               # `total` and `current` let the dashboard draw progress without
               # pulling the whole log every poll.
               "total": 0, "current": "",
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
                search=req.search, library=req.library, series=req.series,
                max_titles=req.max_titles)
            log(f"{len(targets)} target(s)")
            job["total"] = len(targets)
            skip = set(req.skip.split(",")) - {""}
            for rk in targets:
                job["current"] = rk
                result = pipeline.run_title(
                    STORE, source=source,
                    tmdb_key=k.tmdb_key(), audd_token=k.audd_token(),
                    rating_key=rk, skip=skip, audd_budget=AUDD_BUDGET,
                    hub_url=k.hub_url(), hub_miss="index",  # services never prompt
                    level=req.level, log=log)
                _autoshare(result, log)
                job["summary"].append(result)
            job["current"] = ""
            job["status"] = "done"
        except Exception as e:  # noqa: BLE001
            log(f"JOB FAILED: {e}")
            job["current"] = ""
            job["status"] = "failed"


def _autoshare(result: dict, log) -> None:
    """Upload a freshly produced timeline when auto-share is on.

    Only titles this run actually PRODUCED are sent. A timeline fetched from
    the hub is skipped: echoing it straight back is noise in the moderation
    queue and teaches the catalog nothing.

    Sharing must never sink a run, so every failure is logged and swallowed;
    the manual Share button is always still there."""
    if not k.hub_autoshare():
        return
    hub, cid = k.hub_url(), (result.get("key") or "")
    steps = result.get("steps") or {}
    index = str(steps.get("index", ""))
    if not hub or not cid.startswith("tmdb-"):
        return
    if index == "hub" or index.startswith("failed"):
        return
    from ..share import upload_to_hub, upload_token
    # Without a write credential a public hub answers 403, and retrying it once
    # per title would fill the log with the same refusal. Say what to do
    # instead, once, and leave the timeline on disk for a bundle export.
    if not upload_token():
        log("  [share] direct upload needs a hub token this build has no way "
            "to obtain; export a bundle and upload it at the hub's "
            "/contribute page instead")
        return
    try:
        out = upload_to_hub(STORE, cid, hub)
        log(f"  [share] sent to the hub: {out.get('status', 'accepted')}")
    except Exception as e:  # noqa: BLE001 (sharing is best-effort by design)
        log(f"  [share] upload failed: {e}")


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
            "title": doc.get("title"),
            "year": doc.get("year"),
            "series": doc.get("series"),
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
    if not (req.rating_key or req.search or req.library or req.series):
        raise HTTPException(422, "need rating_key, search, library, or series")
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
                    "label": label, "year": r.get("year"),
                    "seriesId": r.get("seriesId"),
                    "series": r.get("grandparentTitle")})
    return {"results": out}


@app.get("/api/libraries")
def api_libraries():
    """Library sections to choose a run target from."""
    if not _origin():
        raise HTTPException(503, "no media server configured; run Setup")
    src = _source()
    if not hasattr(src, "sections"):
        return {"sections": []}
    return {"sections": src.sections()}


@app.get("/api/plan")
def api_plan(library: str):
    """What indexing `library` would do at each level, and what it costs.

    Deliberately synchronous: it is one or two backend requests plus one hub
    request, and the answer is worthless if it arrives after the user has
    already committed to the run."""
    if not _origin():
        raise HTTPException(503, "no media server configured; run Setup")
    from .. import plan as pl
    src = _source()
    # A movie section carries roughly 3x the music cues of a TV section, and
    # cues are what AudD bills, so the section type sets the price.
    kind = next((s["type"] for s in src.sections()
                 if library in (s["key"], s["title"])), "movie")
    try:
        return pl.library_plan(STORE, src, library, kind=kind,
                               hub_url=k.hub_url(), audd=bool(k.audd_token()),
                               audd_headroom=AuddBudget(STORE,
                                                        AUDD_BUDGET).headroom())
    except ValueError as e:  # unknown section
        raise HTTPException(404, str(e))


@app.get("/api/jobs")
def api_jobs(id: int | None = None, log: int = 1):
    if id is not None:
        for j in _jobs:
            if j["id"] == id:
                # log=0: the dashboard polls a running job for its per-title
                # rows every few seconds and does not want thousands of log
                # lines riding along each time.
                return j if log else {kk: v for kk, v in j.items()
                                      if kk != "log"}
        raise HTTPException(404, "no such job")
    # `done`/`total` keep the poll cheap: the dashboard draws a progress bar
    # and the live queue from this, and only fetches a full log on request.
    return [{**{kk: j[kk] for kk in ("id", "target", "status", "created",
                                     "total", "current")},
             "done": len(j["summary"]),
             "level": (j.get("request") or {}).get("level", 1)}
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
        "hubAutoshare": k.hub_autoshare(),
        # Whether this machine can POST to a hub at all. A public hub gates
        # writes in the browser, so without a token the honest offer is a file
        # to upload rather than a button that 403s.
        "hubDirectUpload": bool(_upload_token()),
        # The gate the dashboard keys off: until a server and a TMDb key
        # exist, no run can succeed, so it shows setup and nothing else.
        "ready": bool(_origin()) and bool(k.tmdb_key()),
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
    # "on" or "" (empty deletes the key): settings_store str()-coerces,
    # so a real bool would round-trip False into the truthy "False".
    hub_autoshare: str | None = None


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


class BundleRequest(BaseModel):
    #: Explicit ids, or "" for everything shareable in the store.
    contentIds: list[str] = []


@app.post("/api/export/bundle")
def api_export_bundle(req: BundleRequest):
    """One JSON Lines file for many timelines — the thing you upload to a hub.

    A hub rate limits per request, so sharing a library as individual files
    stops after ten titles. One bundle is one upload however much it carries.
    Chunked when it would exceed what a hub accepts; the response says how many
    files there are and the caller fetches each by name.
    """
    from ..share import export_bundle
    ids = req.contentIds or sorted(
        p.stem for p in STORE.glob("tmdb-*.json") if p.is_file())
    if not ids:
        raise HTTPException(422, "nothing shareable in the store yet")
    out_dir = STORE / "exports"
    files = export_bundle(STORE, ids, out_dir)
    if not files:
        raise HTTPException(422, "no shareable timelines among those ids")
    return {"files": [f.name for f in files],
            "timelines": sum(1 for _ in ids),
            "bytes": sum(f.stat().st_size for f in files)}


@app.get("/api/export/bundle/{name}")
def api_export_bundle_file(name: str):
    """Download one bundle produced by the POST above."""
    if "/" in name or "\\" in name or not name.endswith(".xray.jsonl"):
        raise HTTPException(400, "bad bundle name")
    out = STORE / "exports" / name
    if not out.is_file():
        raise HTTPException(404, "no such bundle; export it first")
    return FileResponse(out, media_type="application/x-ndjson", filename=name)


@app.post("/api/hub/upload/{content_id}")
def api_hub_upload(content_id: str):
    """Direct upload, which only works where this machine holds a hub token.

    A public hub gates writes behind a browser bot check and there is no
    issuance flow for tooling yet, so without a token this used to POST anyway
    and surface the hub's 403 as an opaque 500. Say what is actually true and
    point at the path that works."""
    from ..share import upload_to_hub, upload_token
    hub = k.hub_url()
    if not hub:
        raise HTTPException(503, "no hub URL configured (Settings)")
    if not upload_token():
        raise HTTPException(501, {
            "reason": "direct upload isn't available",
            "detail": "This hub accepts writes from a browser, and there is no "
                      "token for tooling yet. Export a bundle and upload it on "
                      "the hub's /contribute page.",
            "hub": f"{hub.rstrip('/')}/contribute"})
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
#
# The dashboard is STATEFUL rather than a flat list of sections: until a
# server and a TMDb key exist no run can succeed, so setup is all you see;
# after that the page is the run composer, whatever is in flight, and the
# store. The two decisions that actually cost time and money (which level,
# how many titles) are priced from /api/plan before you commit to them.
#
# Plain strings concatenated, deliberately not f-strings: the CSS and JS are
# brace-dense and doubling every one of them is how brace bugs get in.

_STYLE = """<style>
 :root{--paper:#f7f7f4;--raise:#fff;--ink:#16181a;--muted:#6b7370;
  --faint:#9aa19d;--line:#dcdedb;--soft:#e9eae7;--accent:#2f5d55;
  --wash:#e4ede9;--ok:#2e6b46;--warn:#8a5a12;--stop:#8f3a2f;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
 @media(prefers-color-scheme:dark){:root{--paper:#131517;--raise:#1a1d1f;
  --ink:#e8eae8;--muted:#98a09c;--faint:#6b7370;--line:#2b2f31;--soft:#232729;
  --accent:#63a894;--wash:#1e2b28;--ok:#6aab84;--warn:#c69248;--stop:#cf7d6e}}
 *{box-sizing:border-box}
 /* .sec sets display:flex at the same specificity as the UA's
    [hidden]{display:none}, so without this the setup gate hides nothing. */
 [hidden]{display:none!important}
 body{margin:0;background:var(--paper);color:var(--ink);
  font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased}
 main{max-width:62rem;margin:0 auto;padding:1.4rem 1.25rem 5rem;
  display:flex;flex-direction:column;gap:1.6rem}
 header{max-width:62rem;margin:0 auto;padding:1.1rem 1.25rem .2rem;
  display:flex;justify-content:space-between;align-items:baseline;
  gap:1rem;flex-wrap:wrap}
 .brand{display:flex;align-items:center;gap:.55rem;font-weight:640;
  font-size:15px;letter-spacing:-.01em}
 .mark{width:17px;height:17px;border-radius:4px;background:var(--accent);
  flex:none;display:grid;place-items:center}
 .mark i{display:block;width:9px;height:1.5px;background:var(--paper);
  box-shadow:0 -3.5px 0 var(--paper),0 3.5px 0 var(--paper)}
 .stat{font:12px var(--mono);color:var(--faint);font-variant-numeric:tabular-nums}
 h2{font-size:14px;font-weight:640;margin:0;letter-spacing:-.008em}
 p.sub{margin:.15rem 0 0;color:var(--muted);font-size:13px}
 .sec{display:flex;flex-direction:column;gap:.85rem}
 .card{border:1px solid var(--line);border-radius:9px;padding:.9rem 1rem;
  background:var(--raise)}
 .row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}
 .spread{display:flex;justify-content:space-between;align-items:center;
  gap:1rem;flex-wrap:wrap}
 .meta{color:var(--muted);font-size:12.5px}
 .mono{font:11.5px var(--mono);color:var(--faint)}
 .ok{color:var(--ok)}.warn{color:var(--warn)}.miss{color:var(--faint)}
 input,select{padding:.45rem .6rem;border:1px solid var(--line);border-radius:6px;
  background:var(--raise);color:var(--ink);font:13px var(--mono);min-width:0}
 input:focus-visible,select:focus-visible,button:focus-visible{outline:2px solid
  var(--accent);outline-offset:1px}
 button{padding:.45rem .8rem;border-radius:6px;background:var(--accent);
  color:var(--paper);border:1px solid var(--accent);cursor:pointer;
  font:600 13px ui-sans-serif,system-ui,sans-serif;white-space:nowrap}
 button.ghost{background:transparent;color:var(--ink);border-color:var(--line)}
 button.link{background:none;border:none;color:var(--muted);padding:.4rem .2rem;
  font-weight:500;text-decoration:underline;text-underline-offset:3px}
 button.sm{padding:.24rem .55rem;font-size:12px}
 button[disabled]{opacity:.45;cursor:not-allowed}
 a.ghost{padding:.24rem .55rem;font-size:12px;border:1px solid var(--line);
  border-radius:6px;color:var(--ink);text-decoration:none}
 pre{background:var(--soft);padding:.7rem .8rem;border-radius:7px;font-size:12px;
  overflow:auto;max-height:22rem;margin:0}
 .code{font:700 26px var(--mono);letter-spacing:6px}
 /* setup steps */
 .step{display:flex;gap:.85rem;padding:.9rem 1rem;border:1px solid var(--line);
  border-radius:9px;align-items:flex-start;background:var(--raise)}
 .step.done{border-color:var(--soft)}
 .step.now{border-color:var(--accent);background:var(--wash)}
 .step.opt{border-style:dashed;border-color:var(--soft);background:transparent}
 .bul{width:20px;height:20px;border-radius:50%;flex:none;display:grid;
  place-items:center;font:600 11px var(--mono);margin-top:1px}
 .step.done .bul{background:var(--ok);color:var(--paper)}
 .step.now .bul{background:var(--accent);color:var(--paper)}
 .step.opt .bul{background:var(--soft);color:var(--faint)}
 .step .body{flex:1;min-width:0;display:flex;flex-direction:column;gap:.5rem}
 .step p{margin:0;color:var(--muted);font-size:13px;max-width:36rem}
 .tag{font:600 10.5px var(--mono);letter-spacing:.08em;text-transform:uppercase;
  padding:.28rem .45rem;border-radius:4px;flex:none}
 .tag.g{background:var(--wash);color:var(--accent)}
 .tag.n{background:var(--soft);color:var(--faint)}
 /* tiers */
 .tiers{display:grid;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));
  gap:.7rem}
 .tier{border:1px solid var(--line);border-radius:9px;padding:.9rem 1rem;
  display:flex;flex-direction:column;gap:.5rem;cursor:pointer;text-align:left;
  background:var(--raise);color:inherit;font:inherit;
  /* these are buttons, so they inherit button{white-space:nowrap}; without
     this the description cannot wrap and the page scrolls sideways */
  white-space:normal;
  transition:border-color .15s,background .15s}
 .tier:hover{border-color:var(--muted)}
 .tier.on{border-color:var(--accent);background:var(--wash);
  box-shadow:inset 0 0 0 1px var(--accent)}
 .tier b{font-size:13.5px;font-weight:640}
 .tier p{margin:0;color:var(--muted);font-size:12.5px;flex:1}
 .tier .cost{display:flex;gap:.9rem;font:12px var(--mono);
  border-top:1px solid var(--soft);padding-top:.5rem;
  font-variant-numeric:tabular-nums}
 .tier .cost i{font-style:normal;color:var(--faint)}
 .radio{width:14px;height:14px;border-radius:50%;border:1.5px solid var(--line);
  flex:none;display:grid;place-items:center}
 .tier.on .radio{border-color:var(--accent)}
 .tier.on .radio::after{content:"";width:7px;height:7px;border-radius:50%;
  background:var(--accent)}
 /* coverage bar */
 .cov{display:flex;height:7px;border-radius:4px;overflow:hidden;
  background:var(--soft)}
 .cov span{display:block}
 .cov .f{background:var(--accent)}
 .cov .s{background:var(--accent);opacity:.45}
 .cov .h{background:var(--ok);opacity:.6}
 .key{display:flex;gap:.9rem;flex-wrap:wrap;font-size:12.5px;color:var(--muted);
  font-variant-numeric:tabular-nums}
 .key i{width:8px;height:8px;border-radius:2px;display:inline-block;
  margin-right:.35rem}
 /* progress + queue */
 .track{height:5px;border-radius:3px;background:var(--soft);overflow:hidden}
 .fill{height:100%;background:var(--accent);border-radius:3px;
  transition:width .4s ease}
 .q{display:grid;grid-template-columns:1.1rem 1fr auto;gap:.7rem;
  align-items:center;padding:.4rem 0;border-bottom:1px solid var(--soft);
  font-size:13px}
 .q:last-child{border-bottom:none}
 .q .ic{font:11px var(--mono);text-align:center}
 .q.done .ic{color:var(--ok)}.q.live .ic{color:var(--accent)}
 .q.bad .ic{color:var(--warn)}.q.bad .dt{color:var(--warn)}
 .q .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .q.live .nm{font-weight:600}
 .q .dt{font:11.5px var(--mono);color:var(--faint);white-space:nowrap}
 .pulse{animation:blink 1.4s ease-in-out infinite}
 @keyframes blink{0%,100%{opacity:1}50%{opacity:.35}}
 /* store */
 .scroll{overflow-x:auto}
 table{border-collapse:collapse;width:100%;font-size:13px}
 th{text-align:left;font:600 10.5px var(--mono);letter-spacing:.1em;
  text-transform:uppercase;color:var(--faint);padding:0 .6rem .5rem 0;
  border-bottom:1px solid var(--line)}
 td{padding:.5rem .6rem .5rem 0;border-bottom:1px solid var(--soft);
  vertical-align:middle}
 tr:last-child td{border-bottom:none}
 td.acts{text-align:right;white-space:nowrap}
 .chips{display:flex;gap:.28rem;flex-wrap:wrap}
 .chip{font:600 10.5px var(--mono);letter-spacing:.04em;padding:.3rem .42rem;
  border-radius:4px;background:var(--wash);color:var(--accent)}
 .chip.off{background:transparent;color:var(--faint);
  box-shadow:inset 0 0 0 1px var(--soft)}
 /* A missing block you can fill: dashed to read as an absence, not a state.
    Only rendered when the title has a server key to run against. */
 button.chip{border:0;cursor:pointer;font-family:var(--mono)}
 button.chip.add{background:transparent;color:var(--accent);
  box-shadow:inset 0 0 0 1px var(--soft)}
 button.chip.add:hover{background:var(--wash)}
 button.chip.add.paid{color:var(--warn)}
 .note{display:flex;justify-content:space-between;align-items:center;gap:1rem;
  flex-wrap:wrap;padding:.7rem .9rem;border-radius:8px;background:var(--wash);
  font-size:13px;color:var(--accent)}
 @media(prefers-reduced-motion:reduce){*{animation:none!important;
  transition:none!important}}
</style>"""

_HEAD = ('<!doctype html><meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width,initial-scale=1">')

_LOGIN_PAGE = _HEAD + "<title>OpenXray sign-in</title>" + _STYLE + r"""
<header><div class="brand"><span class="mark"><i></i></span> OpenXray</div></header>
<main>
 <div class="card">
  <h2>Paste the web UI token</h2>
  <p class="sub">It is printed in the container logs:
   <span class="mono">docker compose logs orchestrator</span></p>
  <form class="row" style="margin-top:.8rem" onsubmit="login(this);return false">
   <input name="token" style="flex:1" placeholder="web UI token" autofocus>
   <button>Sign in</button>
  </form>
  <div class="warn" id="err" style="margin-top:.5rem"></div>
 </div>
</main>
<script>
async function login(f){
 const r = await fetch('api/login', {method:'POST',
   headers:{'content-type':'application/json'},
   body: JSON.stringify({token: f.token.value.trim()})});
 if(r.ok) location = '.';
 else document.getElementById('err').textContent = (await r.json()).detail;
 return false;
}
</script>"""

_DASH_PAGE = _HEAD + "<title>OpenXray</title>" + _STYLE + r"""
<header>
 <div class="brand"><span class="mark"><i></i></span> OpenXray</div>
 <div class="stat" id="stat"></div>
</header>
<main>
 <div id="setupView" class="sec" hidden></div>
 <div id="runView" class="sec" hidden></div>
 <div id="jobView" class="sec"></div>
 <div id="storeView" class="sec" hidden></div>
 <pre id="out" hidden></pre>
</main>
<script>
const $ = id => document.getElementById(id);

// Actions carrying DATA are wired through data-* attributes and this one
// delegated listener, never an inline onclick. HTML-escaping is not enough
// inside a JS string inside an attribute: the parser decodes &#39; back to a
// quote BEFORE the JS parser runs, so an id containing one would break out
// and execute. dataset hands the decoded text straight to a variable, where
// it is data and stays data.
document.addEventListener('click', ev => {
 const el = ev.target.closest('[data-act]');
 if(!el) return;
 const d = el.dataset;
 if(d.act === 'queue')  return queueOne(d.rk, +d.level);
 if(d.act === 'series') return queueSeries(d.sid, +d.level);
 if(d.act === 'share')  return hubUpload(d.cid);
 if(d.act === 'bundle') return exportBundle(el);
 if(d.act === 'pass')   return queuePass(d.rk, d.pass, d.label);
 if(d.act === 'log')    return showLog(+d.id);
});
const j = r => r.json();
const post = (u, b) => fetch(u, {method:'POST',
  headers:{'content-type':'application/json'},
  body: b ? JSON.stringify(b) : undefined});
// Library titles are third-party strings; never interpolate them raw.
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const plural = (n, w) => n + ' ' + w + (n === 1 ? '' : 's');

function hhmm(sec){
 if(sec < 90) return Math.max(1, Math.round(sec)) + 's';
 if(sec < 5400) return Math.round(sec / 60) + 'm';
 return (sec / 3600).toFixed(sec < 36000 ? 1 : 0) + 'h';
}
function span(range){
 const [lo, hi] = range;
 return lo === hi ? hhmm(lo) : hhmm(lo) + '–' + hhmm(hi);
}
function money(range){
 const [lo, hi] = range;
 if(!hi) return 'free';
 return '$' + lo.toFixed(2) + '–' + hi.toFixed(2);
}

let SETUP = null, LEVEL = 0, LIB = '', PLAN = null, PLANNING = false;
// Music is the only paid step. It rides on the full index but is separable:
// unchecking it sends skip=music, giving a video-only run even with a token.
let MUSIC = true;

// ---- setup -----------------------------------------------------------------

async function loadSetup(){
 SETUP = await j(await fetch('api/setup'));
 const on = SETUP.ready;
 $('setupView').hidden = on;
 $('runView').hidden = !on;
 $('storeView').hidden = !on;
 if(on){ renderRun(); loadStore(); } else renderSetup();
}

function renderSetup(){
 const s = SETUP, server = s.backend === 'jellyfin' ? s.jellyfin : s.plex;
 const connected = !!server.origin;
 const steps = [];

 steps.push(step(connected ? 'done' : 'now', connected ? '✓' : '1',
  'Media server', connected ? '' : 'needed',
  connected
   ? esc(s.backend) + ' · <span class="mono">' + esc(server.origin) + '</span>'
   : 'Sign-in happens on plex.tv. This app gets a token you can revoke, not '
     + 'your password.',
  connected
   ? '<button class="link sm" onclick="serverUI(1)">change</button>'
   : '<div id="serverUI"></div>', !connected));

 steps.push(step(s.tmdbConfigured ? 'done' : (connected ? 'now' : 'opt'),
  s.tmdbConfigured ? '✓' : '2', 'TMDb key',
  s.tmdbConfigured ? '' : 'needed',
  'Free from themoviedb.org. Titles are identified by TMDb id, so nothing '
  + 'runs without one.',
  s.tmdbConfigured ? '' :
   '<div class="row"><input id="tmdbKey" style="flex:1" placeholder="paste key">'
   + '<button onclick="saveKeys()">Save</button></div>'));

 steps.push(step('opt', '+', 'AudD token', 'optional',
  'Names the songs during a full index. Billed per music cue, about $0.005 '
  + 'each. Without it, music is skipped.',
  '<div class="row"><input id="auddKey" style="flex:1" placeholder="AudD token'
  + (s.auddConfigured ? ' (set)' : '') + '">'
  + '<button class="ghost" onclick="saveKeys()">Save</button></div>'));

 // No input here on purpose: the hub is configured, not chosen. Pointing
 // the stack elsewhere is a dev concern and stays on XRAY_HUB_URL.
 steps.push(step(s.hubUrl ? 'done' : 'opt', s.hubUrl ? '✓' : '+',
  'Hub', s.hubUrl ? '' : 'off',
  s.hubUrl
   ? 'Sharing through <span class="mono">' + esc(s.hubUrl) + '</span>. '
     + 'A full index checks it first and downloads a title when someone has '
     + 'already done that one, instead of computing it again.'
   : 'Turned off, so every title is computed locally and nothing is shared.',
  ''));

 $('setupView').innerHTML =
  '<div><h2>Setup</h2><p class="sub">The first two are required.</p></div>'
  + steps.join('');
 if(!connected) serverUI(0);
}

function step(state, bullet, title, tag, body, extra, openNow){
 return '<div class="step ' + state + '"><div class="bul">' + bullet + '</div>'
  + '<div class="body"><div class="spread"><b>' + title + '</b>'
  + (tag ? '<span class="tag ' + (state === 'opt' ? 'n' : 'g') + '">' + tag
           + '</span>' : '')
  + '</div><p>' + body + '</p>' + (extra || '') + '</div></div>';
}

function serverUI(force){
 const el = $('serverUI') || $('setupView');
 const box = $('serverUI');
 if(!box){ renderSetup(); return; }
 box.innerHTML =
  '<div class="row"><button onclick="plexSignIn()">Sign in with Plex</button>'
  + '<span class="meta">or Jellyfin:</span>'
  + '<input id="jfOrigin" placeholder="http://jellyfin:8096" style="flex:1">'
  + '<button class="ghost" onclick="jfQuick()">Quick Connect</button></div>'
  + '<div class="row"><input id="jfUser" placeholder="user">'
  + '<input id="jfPass" type="password" placeholder="password">'
  + '<button class="ghost" onclick="jfPassword()">password sign-in</button></div>'
  + '<div id="flow"></div>';
}

async function plexSignIn(){
 const pin = await j(await post('api/auth/plex/pin'));
 window.open(pin.authUrl, '_blank');
 $('flow').innerHTML = '<div class="card meta">Finish signing in on the Plex '
  + 'tab… <span class="mono">' + esc(pin.code.slice(0, 4)) + '…</span></div>';
 for(let i = 0; i < 120; i++){
  await new Promise(r => setTimeout(r, 3000));
  if((await j(await fetch('api/auth/plex/pin/' + pin.id))).claimed) return pickServer();
 }
}

async function pickServer(){
 const data = await j(await fetch('api/auth/plex/servers'));
 const opts = [];
 for(const srv of data.servers)
  for(const c of srv.connections)
   opts.push('<option value="' + esc(c.uri) + '">' + esc(srv.name) + ' · '
    + esc(c.uri) + (c.local ? ' (local)' : '') + (c.relay ? ' (relay)' : '')
    + '</option>');
 $('flow').innerHTML = '<div class="card row"><select id="srvPick" style="flex:1">'
  + opts.join('') + '</select><button onclick="saveServer()">Use this server</button></div>';
}

async function saveServer(){
 const r = await j(await post('api/auth/plex/origin', {uri: $('srvPick').value}));
 $('flow').innerHTML = r.reachable ? '' : '<div class="card warn">Saved, but this '
  + 'address is not reachable from the container: pick a different connection '
  + '(a LAN address can be dead from here while the remote one works).</div>';
 loadSetup();
}

async function jfQuick(){
 const origin = $('jfOrigin').value.trim();
 if(!origin) return;
 const r = await j(await post('api/auth/jellyfin/quickconnect', {origin}));
 if(!r.enabled){ $('flow').innerHTML = '<div class="card warn">Quick Connect is '
  + 'disabled on this server; use password sign-in.</div>'; return; }
 $('flow').innerHTML = '<div class="card">Enter this code in Jellyfin '
  + '(Settings → Quick Connect):<div class="code">' + esc(r.code) + '</div></div>';
 for(let i = 0; i < 120; i++){
  await new Promise(res => setTimeout(res, 3000));
  if((await j(await fetch('api/auth/jellyfin/quickconnect/' + r.secret))).claimed){
   $('flow').innerHTML = ''; return loadSetup();
  }
 }
}

async function jfPassword(){
 const r = await post('api/auth/jellyfin/password', {
  origin: $('jfOrigin').value.trim(), username: $('jfUser').value,
  password: $('jfPass').value});
 if(r.ok) loadSetup();
 else $('flow').innerHTML = '<div class="card warn">'
  + esc((await r.json()).detail || 'sign-in failed') + '</div>';
}

async function saveKeys(){
 const body = {};
 for(const [id, field] of [['tmdbKey','tmdb_key'], ['auddKey','audd_token']]){
  const el = $(id);
  if(el && el.value.trim()) body[field] = el.value.trim();
 }
 if(!Object.keys(body).length) return;
 await fetch('api/settings', {method:'PUT',
  headers:{'content-type':'application/json'}, body: JSON.stringify(body)});
 loadSetup();
}

// ---- run composer ----------------------------------------------------------

async function renderRun(){
 const libs = await j(await fetch('api/libraries')).catch(() => ({sections:[]}));
 const opts = (libs.sections || []).map(s =>
  '<option value="' + esc(s.title) + '"' + (s.title === LIB ? ' selected' : '')
  + '>' + esc(s.title) + '</option>').join('');
 $('runView').innerHTML =
  '<div class="row"><input id="q" style="flex:1" placeholder="Search a title…"'
  + ' onkeydown="if(event.key===\'Enter\')doSearch()">'
  + '<button class="ghost" onclick="doSearch()">Search</button></div>'
  + '<div id="results"></div>'
  + '<div class="card sec"><div class="row"><b>Whole library</b>'
  + '<select id="lib" onchange="pickLib()" style="flex:1">'
  + '<option value="">choose…</option>' + opts + '</select></div>'
  + '<div id="plan"></div></div>';
 if(LIB) pickLib();
}

async function pickLib(){
 LIB = $('lib').value;
 PLAN = null;
 if(!LIB){ $('plan').innerHTML = ''; return; }
 $('plan').innerHTML = '<p class="sub">Checking what you already have…</p>';
 PLANNING = true;
 const r = await fetch('api/plan?library=' + encodeURIComponent(LIB));
 PLANNING = false;
 if(!r.ok){
  $('plan').innerHTML = '<p class="sub warn">'
   + esc((await r.json()).detail) + '</p>';
  return;
 }
 PLAN = await r.json();
 renderPlan();
}

function renderPlan(){
 const p = PLAN, n = p.distinct || 1;
 const pct = x => (100 * x / n).toFixed(2) + '%';
 const lv = p.levels[String(LEVEL)];
 const hubBit = p.hubChecked
  ? (p.levels['1'].fromHub
     ? plural(p.levels['1'].fromHub, 'title') + ' already on the hub'
     : 'no hub coverage for these')
  : (SETUP.hubUrl ? 'hub unreachable' : 'no hub configured');

 $('plan').innerHTML =
  '<div class="spread"><h2>' + esc(p.library) + '</h2>'
  + '<span class="mono">' + plural(p.total, 'title') + '</span></div>'
  + '<div class="cov" style="margin:.6rem 0 .5rem">'
  + '<span class="f" style="width:' + pct(p.haveFull) + '"></span>'
  + '<span class="s" style="width:' + pct(p.haveSeed) + '"></span>'
  + '<span class="h" style="width:' + pct(p.levels['1'].fromHub) + '"></span>'
  + '</div>'
  + '<div class="key">'
  + keyItem('var(--accent)', p.haveFull, 'fully indexed')
  + keyItem('var(--accent);opacity:.45', p.haveSeed, 'seeded')
  + keyItem('var(--ok);opacity:.6', p.levels['1'].fromHub, 'on the hub')
  + keyItem('var(--soft)', p.levels['1'].todo, 'not indexed')
  + '</div>'
  + (p.unidentified ? '<p class="sub">' + plural(p.unidentified, 'title')
     + ' have no TMDb match and will be skipped.</p>' : '')
  + '<div class="tiers" style="margin-top:.9rem">'
  + tier(0, 'Quick seed', 'Cast, biographies and trivia. Does not read the '
      + 'video file.', p.levels['0'])
  + tier(1, 'Full index', 'Adds per-actor on-screen intervals'
      + (SETUP.auddConfigured ? ' and song names' : '')
      + '. Reads each video file.', p.levels['1'])
  + '</div>'
  + musicRow(p.levels['1'])
  + '<div class="spread" style="margin-top:.9rem">'
  + '<span class="meta">' + esc(hubBit)
  + (LEVEL === 0 ? '. Seeds can be deepened later without redoing this work.'
     : '') + '</span>'
  + '<button onclick="queueLibrary()"' + (lv.todo ? '' : ' disabled') + '>'
  + (lv.todo ? (LEVEL ? 'Index ' : 'Seed ') + plural(lv.todo, 'title')
             : 'Nothing to do') + '</button></div>'
  // Phrased without a subject-verb agreement to get right for any count.
  + (LEVEL === 1 && p.levels['1'].hubCouldServe
     ? '<p class="sub">The hub has full timelines for '
       + plural(p.levels['1'].hubCouldServe, 'seeded title')
       + ', but seeds are upgraded locally, so they count as work.</p>' : '');
}

function keyItem(color, n, label){
 return '<span><i style="background:' + color + '"></i>' + n + ' ' + label + '</span>';
}

function tier(level, title, body, lv){
 // Level 1 only costs money while music is on; level 0 never does.
 const cash = (level === 1 && MUSIC) ? lv.dollars : [0, 0];
 return '<button class="tier' + (LEVEL === level ? ' on' : '') + '"'
  + ' onclick="setLevel(' + level + ')"><div class="spread"><b>' + title + '</b>'
  + '<span class="radio"></span></div><p>' + body + '</p>'
  + '<div class="cost"><span>' + (lv.todo ? span(lv.seconds) : '—')
  + ' <i>total</i></span><span>' + money(cash) + '</span></div></button>';
}

function musicRow(lv){
 if(LEVEL !== 1 || !PLAN.auddAvailable || !lv.todo) return '';
 // The cap is a local spend guard, not an AudD tier: AudD's 300 free
 // requests are one-time at signup, so say whose limit this is.
 const cap = MUSIC && lv.titlesBeforeCap !== null
  ? '<p class="sub warn">Your spend cap (' + PLAN.auddHeadroom
    + ' calls left this month) stops music after about '
    + plural(lv.titlesBeforeCap, 'title') + '. Raise XRAY_AUDD_BUDGET to '
    + 'change it.</p>'
  : '';
 return '<label class="row" style="margin-top:.7rem;font-size:13px">'
  + '<input type="checkbox"' + (MUSIC ? ' checked' : '')
  + ' onchange="setMusic(this.checked)"> Name songs'
  + (MUSIC ? '<span class="meta">' + lv.cues[0] + '–' + lv.cues[1]
             + ' cues, $0.005 each</span>' : '')
  + '</label>' + cap;
}

function setMusic(on){ MUSIC = on; if(PLAN) renderPlan(); }
function setLevel(l){ LEVEL = l; if(PLAN) renderPlan(); }

// A full index with music off is a video-only run: the pipeline's own skip
// list, not a second code path.
function runSkip(level){ return (level === 1 && !MUSIC) ? 'music' : ''; }

async function queueLibrary(){
 const r = await post('api/run',
  {library: LIB, level: LEVEL, skip: runSkip(LEVEL)});
 if(!r.ok) return alert((await r.json()).detail);
 poll();
}

async function doSearch(){
 const q = $('q').value.trim();
 if(!q) return;
 $('results').innerHTML = '<p class="sub">searching…</p>';
 const r = await fetch('api/search?q=' + encodeURIComponent(q));
 if(!r.ok){ $('results').innerHTML = '<p class="sub warn">'
  + esc((await r.json()).detail) + '</p>'; return; }
 const data = await r.json();
 if(!data.results.length){
  $('results').innerHTML = '<p class="sub">no matches in the library</p>'; return; }
 $('results').innerHTML = '<div class="card">'
  + data.results.map(resultRow).join('') + '</div>';
}

function resultRow(x){
 const rk = 'data-rk="' + esc(x.ratingKey) + '"';
 const row = '<div class="spread" style="padding:.25rem 0"><span>' + esc(x.label)
  + (x.year ? ' <span class="meta">(' + esc(x.year) + ')</span>' : '')
  + '</span><span class="row"><span class="mono">' + esc(x.type) + '</span>'
  + '<button class="ghost sm" data-act="queue" ' + rk + ' data-level="0">Seed</button>'
  + '<button class="sm" data-act="queue" ' + rk + ' data-level="1">Full index</button>'
  + '</span></div>';
 // An episode brings its whole show with it: indexing TV one episode at a
 // time is the tedious path this avoids.
 if(!x.seriesId) return row;
 const sid = 'data-sid="' + esc(x.seriesId) + '"';
 return row + '<div class="meta" style="padding:0 0 .35rem">'
  + 'All of ' + esc(x.series || 'this show') + ': '
  + '<button class="link sm" data-act="series" ' + sid + ' data-level="0">seed</button>'
  + ' · <button class="link sm" data-act="series" ' + sid
  + ' data-level="1">full index</button></div>';
}

async function queueSeries(seriesId, level){
 const r = await post('api/run',
  {series: seriesId, level, skip: runSkip(level)});
 if(!r.ok) return alert((await r.json()).detail);
 $('results').innerHTML = ''; $('q').value = '';
 poll();
}

async function queueOne(ratingKey, level){
 const r = await post('api/run',
  {rating_key: ratingKey, level, skip: runSkip(level)});
 if(!r.ok) return alert((await r.json()).detail);
 $('results').innerHTML = ''; $('q').value = '';
 poll();
}

// The pipeline's four passes. Running one alone is "skip the other three",
// which is the same `skip` a whole-library run already takes — so a single
// pass on one title needs no endpoint of its own.
const PASSES = ['index', 'people', 'trivia', 'music'];

async function queuePass(ratingKey, pass, label){
 // Money is the only thing worth interrupting for; the free passes just run.
 if(pass === 'music' && !confirm(
      'Identify songs in ' + (label || 'this title') + '?\n\n'
      + 'Billed per music cue, about $0.005 each — roughly $0.10–$0.20 '
      + 'for a feature film. The other passes are free.')) return;
 const r = await post('api/run', {
   rating_key: ratingKey, level: 1,
   skip: PASSES.filter(p => p !== pass).join(',')});
 if(!r.ok) return alert((await r.json()).detail);
 $('results').innerHTML = ''; $('q').value = '';
 poll();
}

// ---- jobs ------------------------------------------------------------------

const STEP_LABEL = {index:'indexing', people:'cast', trivia:'trivia', music:'music'};

async function poll(){
 const jobs = await j(await fetch('api/jobs'));
 const live = jobs.find(x => x.status === 'running' || x.status === 'queued');
 if(!live){
  const last = jobs[0];
  $('jobView').innerHTML = last
   ? '<div class="spread"><span class="meta">Last run: ' + esc(last.target || '')
     + ' · ' + esc(last.status) + ' · ' + last.done + '/' + last.total
     + '</span><button class="link sm" data-act="log" data-id="' + last.id
   + '">show log</button></div>'
   : '';
  loadStore();
  return;
 }
 const job = await j(await fetch('api/jobs?log=0&id=' + live.id));
 const total = job.total || 0, done = (job.summary || []).length;
 const pct = total ? (100 * done / total).toFixed(1) : 0;
 const rows = (job.summary || []).slice(-6).map(rowFor).join('')
  + (job.current && done < total
     ? '<div class="q live"><span class="ic pulse">●</span>'
       + '<span class="nm">' + esc(job.current) + '</span>'
       + '<span class="dt">working…</span></div>' : '');
 $('jobView').innerHTML =
  '<div class="sec"><div class="spread"><h2>'
  + (job.request && job.request.level ? 'Indexing ' : 'Seeding ')
  + esc(job.target || '') + '</h2><span class="mono">' + done + ' / ' + total
  + '</span></div><div class="track"><div class="fill" style="width:' + pct
  + '%"></div></div><div>' + rows + '</div>'
  + '<div><button class="link sm" data-act="log" data-id="' + job.id
  + '">show full log</button></div></div>';
 loadStore();
}

function rowFor(s){
 const steps = s.steps || {};
 const failed = Object.entries(steps).filter(([, v]) => String(v).startsWith('failed'));
 const noId = String(steps.index || '').includes('no content identity');
 if(noId) return '<div class="q bad"><span class="ic">!</span><span class="nm">'
  + esc(s.title) + '</span><span class="dt">no TMDb match · skipped</span></div>';
 const cls = failed.length ? 'bad' : 'done';
 const detail = failed.length
  ? failed.map(([kk]) => kk).join(', ') + ' failed'
  : (steps.index === 'hub' ? 'from the hub' : esc(s.key));
 return '<div class="q ' + cls + '"><span class="ic">'
  + (failed.length ? '!' : '✓') + '</span><span class="nm">' + esc(s.title)
  + '</span><span class="dt">' + detail + '</span></div>';
}

async function showLog(id){
 const jb = await j(await fetch('api/jobs?id=' + id));
 $('out').hidden = false;
 $('out').textContent = (jb.log || []).join('\n') || '(no log yet)';
}

// ---- store -----------------------------------------------------------------

async function loadStore(){
 const s = await j(await fetch('api/status'));
 $('stat').textContent = s.backend + ' · ' + (s.origin || 'no server')
  + ' · AudD ' + s.auddUsed + '/' + (s.auddMonthly || '∞') + ' this month';
 const seeds = s.titles.filter(t => !t.blocks.faces).length;
 const rows = s.titles.map(t => {
  const rk = (t.lookup[0] || '').split(':')[1];
  // A present block is a state; a missing one is an offer. Filling it runs
  // that pass ALONE (skip = the other three), which is why every block is
  // offered and not just the paid one — the pipeline already takes `skip`,
  // this only stops the dashboard hardcoding it to music-or-nothing.
  //
  // Needs a server key: every pass runs through the pipeline against the
  // media server, so a timeline fetched from the hub with no local copy has
  // nothing to run against and stays a plain chip.
  const chip = (label, pass, on, paid) => {
   if (on) return '<span class="chip">' + label + '</span>';
   if (!rk) return '<span class="chip off">' + label + '</span>';
   return '<button class="chip add' + (paid ? ' paid' : '') + '"'
    + ' data-act="pass" data-rk="' + esc(rk) + '" data-pass="' + pass + '"'
    + ' data-label="' + esc(storeLabel(t).replace(/<[^>]*>/g, '')) + '"'
    + ' title="' + (paid
       ? 'Identify songs — billed per cue, about $0.10–0.20 for a feature'
       : 'Add ' + label + ' to this title — free') + '">+ ' + label + '</button>';
  };
  return '<tr><td>' + storeLabel(t)
   + '<div class="mono">' + esc(t.contentId) + '</div>'
   + '</td><td><div class="chips">'
   + chip('cast', 'people', t.blocks.people)
   + chip('faces', 'index', t.blocks.faces)
   + chip('music', 'music', t.blocks.music, true)
   + chip('trivia', 'trivia', t.blocks.trivia)
   + '</div></td><td class="acts">'
   + (!t.blocks.faces && rk
      ? '<button class="ghost sm" data-act="queue" data-rk="' + esc(rk)
        + '" data-level="1">Deepen</button> ' : '')
   + '<a class="ghost" href="api/export/' + encodeURIComponent(t.contentId)
   + '">export</a>'
   // Direct upload only where this machine holds a hub token; otherwise the
   // button could only ever report the hub's refusal, so it isn't offered.
   + (SETUP && SETUP.hubDirectUpload
      ? ' <button class="sm" data-act="share" data-cid="'
        + esc(t.contentId) + '">Share</button>' : '')
   + '</td></tr>';
 }).join('');
 const auto = SETUP && SETUP.hubAutoshare;
 $('storeView').innerHTML =
  '<div class="spread"><h2>Store</h2><span class="mono">'
  + plural(s.titles.length, 'timeline') + '</span></div>'
  // Auto-share can only work where direct upload can. Offering the toggle
  // otherwise promises a thing that ends in the hub's 403, once per title.
  + (SETUP && SETUP.hubUrl && SETUP.hubDirectUpload
     ? '<label class="row" style="font-size:13px"><input type="checkbox"'
       + (auto ? ' checked' : '') + ' onchange="setAutoshare(this.checked)">'
       + ' Share new timelines automatically'
       + '<span class="meta">' + (auto
          ? 'each title is sent for review as it finishes'
          : 'off: use Share per title') + '</span></label>'
     : '')
  + (SETUP && SETUP.hubUrl && !SETUP.hubDirectUpload && s.titles.length
     ? '<div class="note"><span>Contributing is two steps: build a bundle '
       + 'here, then upload it on the hub&rsquo;s contribute page. One bundle '
       + 'covers your whole store and counts as a single upload.</span>'
       + '<button class="sm" data-act="bundle">Export bundle</button></div>'
     : '')
  + '<div id="bundleOut" class="note" hidden></div>'
  + (s.titles.length
     ? '<div class="scroll"><table><thead><tr><th>Title</th><th>Contains</th>'
       + '<th></th></tr></thead><tbody>' + rows + '</tbody></table></div>'
       + (seeds ? '<div class="note"><span><b>' + plural(seeds, 'title')
          + '</b> ' + (seeds === 1 ? 'has' : 'have') + ' no face intervals '
          + 'yet. Deepen to add them'
          + (SETUP && SETUP.auddConfigured ? ' and the song names' : '')
          + '.</span></div>' : '')
     : '<p class="sub">Nothing indexed yet.</p>')
  + '<div class="row"><input id="importSrc" style="flex:1" '
  + 'placeholder="import URL (hub or shared file)">'
  + '<button class="ghost" onclick="doImport()">Import</button>'
  + '<button class="ghost" onclick="doValidate()">Validate all</button></div>';
}

// Season/episode come from the contentId, not the doc: one source of truth,
// so the label can never disagree with the identity the file is stored under.
const EP_RE = /^tmdb-tv-\d+-s(\d{2})e(\d{2})$/;

function storeLabel(t){
 if(!t.title) return '<b>' + esc(t.contentId) + '</b>';
 const m = EP_RE.exec(t.contentId);
 if(t.series && m) return '<b>' + esc(t.series) + '</b> S' + m[1] + 'E' + m[2]
  + ' · ' + esc(t.title);
 if(t.series) return '<b>' + esc(t.series) + '</b> · ' + esc(t.title);
 return '<b>' + esc(t.title) + '</b>'
  + (t.year ? ' <span class="meta">(' + esc(t.year) + ')</span>' : '');
}

async function setAutoshare(on){
 // Empty string deletes the key: settings_store's own off switch.
 await fetch('api/settings', {method:'PUT',
  headers:{'content-type':'application/json'},
  body: JSON.stringify({hub_autoshare: on ? 'on' : ''})});
 SETUP.hubAutoshare = on;
 loadStore();
}

async function hubUpload(cid){
 const r = await post('api/hub/upload/' + encodeURIComponent(cid));
 $('out').hidden = false;
 $('out').textContent = JSON.stringify(await r.json(), null, 1);
}
async function exportBundle(btn){
 const box = $('bundleOut');
 btn.disabled = true; btn.textContent = 'Bundling…';
 box.hidden = false; box.innerHTML = '<span>Writing the bundle…</span>';
 try {
  const r = await post('api/export/bundle', {contentIds: []});
  const d = await r.json();
  if(!r.ok){
   box.innerHTML = '<span>' + esc((d.detail && d.detail.reason) || d.detail
     || 'export failed') + '</span>';
   return;
  }
  // Chunked when a hub would refuse the whole thing; each file uploads
  // separately, so link every one rather than only the first.
  const kb = Math.round(d.bytes / 1024);
  const links = d.files.map(n => '<a class="ghost" href="api/export/bundle/'
    + encodeURIComponent(n) + '">' + esc(n) + '</a>').join(' ');
  box.innerHTML = '<span>' + plural(d.timelines, 'timeline') + ' in '
   + plural(d.files.length, 'file') + ' (' + kb + ' KB). Download, then upload '
   + 'at <a href="' + esc((SETUP.hubUrl || '').replace(/\/$/, ''))
   + '/contribute" target="_blank" rel="noopener noreferrer">the hub&rsquo;s '
   + 'contribute page</a>.</span><span>' + links + '</span>';
 } finally {
  btn.disabled = false; btn.textContent = 'Export bundle';
 }
}
async function doImport(){
 const src = $('importSrc').value.trim();
 if(!src) return;
 const r = await post('api/import', {src});
 $('out').hidden = false;
 $('out').textContent = JSON.stringify(await r.json(), null, 1);
 loadStore();
}
async function doValidate(){
 const r = await j(await fetch('api/validate'));
 $('out').hidden = false;
 $('out').textContent = r.results.map(x =>
  x.file + ': ' + (x.valid ? 'VALID' : 'INVALID: ' + x.error)).join('\n')
  || '(store is empty)';
}

loadSetup();
setInterval(poll, 4000);
</script>"""


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return _LOGIN_PAGE


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return _DASH_PAGE
