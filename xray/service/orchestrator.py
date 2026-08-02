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
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .. import engines
from .. import keys as k
from .. import faceprints as fpmod, voiceprints as vpmod
from .. import pipeline, prints, settings_store as ss, store as st
from ..budget import AuddBudget
from .. import progress
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

app = FastAPI(title="OpenXray Generator", version="0.1.0")

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
    #: With `series`, narrows to one season. 0 is Specials, so this is
    #: None-vs-set rather than falsy — see pipeline.enumerate_targets.
    season: int | None = None
    max_titles: int = 0
    skip: str = ""
    level: int = 1  # 0 = video-free seed, 1 = full index


def _submit(req: RunRequest) -> dict:
    with _lock:
        target = (req.search or req.library or req.series or req.rating_key)
        if req.series and req.season is not None:
            target = f"{target} S{req.season:02d}"   # a season is not the show
        job = {"id": len(_jobs) + 1,
               "target": target,
               "request": req.model_dump(), "status": "queued",
               # `total` and `current` let the dashboard draw progress without
               # pulling the whole log every poll. `phase`/`phaseDone`/
               # `phaseTotal` do the same one level down, inside a single
               # title: a feature-length index is minutes of work that would
               # otherwise sit at 0/1 looking indistinguishable from stuck.
               "total": 0, "current": "", "currentTitle": "",
               "phase": "", "phaseDone": 0, "phaseTotal": 0, "phaseFrac": 0.0,
               "cancel": False,
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
                season=req.season, max_titles=req.max_titles)
            log(f"{len(targets)} target(s)")
            job["total"] = len(targets)
            skip = set(req.skip.split(",")) - {""}

            def on_progress(ev, job=job):
                """Sub-title events from the passes. Last one wins; this is a
                position, not a stream, so nothing accumulates."""
                if "title" in ev:
                    job["currentTitle"] = str(ev["title"])
                    return
                job["phase"] = str(ev.get("phase") or "")
                job["phaseDone"] = int(ev.get("done") or 0)
                job["phaseTotal"] = int(ev.get("total") or 0)
                # Monotonic: cast matching and writing report no count, so a
                # raw reading would send the bar back to zero when the face
                # loop hands off to them.
                job["phaseFrac"] = progress.advance(job["phaseFrac"], ev)
                # Checked here because a marker is the only thing that fires
                # regularly inside a long pass; anywhere else and a stop would
                # not land until the pass ended on its own.
                if job.get("cancel"):
                    raise pipeline.Cancelled("stopped")

            for rk in targets:
                if job.get("cancel"):
                    log("stopped before " + rk)
                    break
                job["current"] = rk
                # Cleared per title: a phase left over from the previous one
                # would show the wrong stage for however long the next title
                # takes to reach its first marker.
                job["currentTitle"] = ""
                job["phase"], job["phaseDone"], job["phaseTotal"] = "", 0, 0
                job["phaseFrac"] = 0.0
                result = pipeline.run_title(
                    STORE, source=source,
                    tmdb_key=k.tmdb_key(), audd_token=k.audd_token(),
                    rating_key=rk, skip=skip, audd_budget=AUDD_BUDGET,
                    hub_url=k.hub_url(), hub_miss="index",  # services never prompt
                    level=req.level, log=log, progress=on_progress)
                _autoshare(result, log)
                job["summary"].append(result)
            job["current"] = job["currentTitle"] = ""
            job["phase"], job["phaseDone"], job["phaseTotal"] = "", 0, 0
            job["phaseFrac"] = 0.0
            job["status"] = "stopped" if job.get("cancel") else "done"
        except pipeline.Cancelled:
            # A stop is an outcome, not a failure: titles already finished
            # stay in the summary and on disk, and the partial one is simply
            # abandoned (its timeline is only written at the end).
            log("stopped")
            job["current"] = job["currentTitle"] = ""
            job["phase"], job["phaseDone"], job["phaseTotal"] = "", 0, 0
            job["phaseFrac"] = 0.0
            job["status"] = "stopped"
        except Exception as e:  # noqa: BLE001
            log(f"JOB FAILED: {e}")
            job["current"] = job["currentTitle"] = ""
            job["phase"], job["phaseDone"], job["phaseTotal"] = "", 0, 0
            job["phaseFrac"] = 0.0
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

def _speaker_state(doc: dict) -> dict | None:
    """{found, nameable, named, pct} for a diarized title, None if it has none.

    `named` counts voice-derived intervals rather than trusting a flag: the
    timeline is the thing that gets shared, so what it actually carries is the
    truthful answer to "has anyone been named".

    `pct` is the share of DIALOGUE TIME the named speakers cover, and it is
    the honest progress number: five names out of 24 sounds like 20% done,
    but a film's dialogue is top-heavy -- on the first real title those five
    covered 47%. Counting speakers understates; counting minutes doesn't.
    """
    cid = doc.get("contentId")
    if "speakers" not in (doc.get("provenance") or {}) or not cid:
        return None
    clusters = vpmod.read_clusters(STORE, cid)
    if not clusters:
        return None
    spk = clusters.get("speakers") or []
    named = {iv.get("actorId") for iv in (doc.get("actorIntervals") or [])
             if iv.get("source") == "voice"}
    named_spk = set()
    try:
        named_spk = set(json.loads(_names_path(STORE, cid).read_text()))
    except (OSError, ValueError):
        pass                       # no names yet, or unreadable: pct stays 0
    total = sum(s.get("seconds") or 0 for s in spk)
    covered = sum(s.get("seconds") or 0 for s in spk
                  if s.get("speaker") in named_spk)
    return {"found": len(spk),
            "nameable": sum(1 for s in spk if s.get("enrollable")),
            "named": len(named),
            "pct": round(100 * covered / total) if total else 0}


def _face_state(doc: dict) -> dict | None:
    """{found, nameable, named, pct} for an indexed title, None without one.

    Same shape as the speaker state so the Store row can render either, but
    `named` means something slightly different: a face cluster can be settled
    either because a person named it or because it matched a cast photo, and
    both are equally "done" from the screen's point of view. `pct` is the
    share of on-screen time settled, not a count of clusters, for the same
    reason it is for voices -- the leads carry most of the runtime.
    """
    cid = doc.get("contentId")
    if not cid:
        return None
    clusters = fpmod.read_clusters(STORE, cid)
    if not clusters:
        return None
    rows = [c for c in clusters.get("clusters") or [] if c.get("nameable")]
    if not rows:
        return None
    named = _named_faces(STORE, cid)

    def settled(c):
        m = c.get("matched")
        return (str(c["cluster"]) in named
                or bool(m and m.get("sim", 0) >= fpmod.MATCH_THRESHOLD))

    total = sum(c.get("screenSeconds") or 0 for c in rows)
    covered = sum(c.get("screenSeconds") or 0 for c in rows if settled(c))
    return {"found": len(clusters.get("clusters") or []),
            "nameable": len(rows),
            "named": sum(1 for c in rows if settled(c)),
            "pct": round(100 * covered / total) if total else 0}


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
                       for b in ("faces", "people", "music", "trivia",
                                 "speakers")},
            # Speakers is the one block whose presence does NOT mean finished:
            # the pass stores clusters and stops, because naming needs a
            # person. The row has to be able to say "16 speakers, none named",
            # which no other block ever needs to express.
            "speakerState": _speaker_state(doc),
            "faceState": _face_state(doc),
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


def _series_id_for(result: dict) -> str | None:
    """The show a search result belongs to, whether it IS one or is in one."""
    if result.get("type") == "show":
        return str(result.get("ratingKey"))
    return str(result["seriesId"]) if result.get("seriesId") else None


def _by_selector(src, stem: str, season: int, episode: int | None) -> list[dict]:
    """Results for "smallville s1e1": find the show by name, pick by number.

    Costs one extra request (series_leaves) and only when a selector was
    actually typed. An unknown show, or a season/episode that does not exist,
    returns nothing here and the caller falls back to a plain search rather
    than showing the user an error for a typo.
    """
    from .. import query as qy
    series_id = next((sid for r in src.search(stem)
                      if (sid := _series_id_for(r))), None)
    if not series_id:
        return []
    leaves = qy.pick(src.series_leaves(series_id), season, episode)
    # series_leaves omits the show title on each leaf for some backends, so
    # carry it over: the label is built from it.
    show = next((r.get("title") for r in src.search(stem)
                 if r.get("type") == "show"), None)
    for lf in leaves:
        lf.setdefault("type", "episode")
        lf["seriesId"] = series_id
        if not lf.get("grandparentTitle"):
            lf["grandparentTitle"] = show
    return leaves


@app.get("/api/search")
def api_search(q: str):
    """Library candidates for a query: the UI shows these and queues the
    user's PICK by ratingKey (no more blind first-match).

    A trailing episode selector ("smallville s1e1", "smallville 1x01",
    "smallville s2") is split off first. Plex matches on titles, so the
    selector would otherwise match nothing: the episodes are called "Pilot"
    and "Metamorphosis"."""
    if not _origin():
        raise HTTPException(503, "no media server configured; run Setup")
    from .. import query as qy
    src = _source()
    stem, season, episode = qy.split_selector(q)
    # `is not None`: season 0 is Specials, a real season.
    results = (_by_selector(src, stem, season, episode)
               if season is not None else [])
    # No selector, or one that matched nothing: plain search on what was typed.
    if not results:
        results = src.search(q)
    out = []
    for r in results:
        typ = r.get("type")
        # Shows are included, not just their episodes. Plex answers a query
        # like "smallville" with the SHOW; its episodes are called "Pilot" and
        # "Metamorphosis", so they match nothing. Dropping shows made those
        # searches come back empty, which read as "not in the library".
        if typ not in ("movie", "episode", "show"):
            continue
        label = r.get("title") or ""
        if typ == "episode" and r.get("grandparentTitle"):
            label = (f"{r['grandparentTitle']} "
                     f"S{int(r.get('season') or 0):02d}"
                     f"E{int(r.get('episode') or 0):02d} · {label}")
        out.append({"ratingKey": r.get("ratingKey"), "type": typ,
                    "label": label, "year": r.get("year"),
                    # A show IS its own series target (series_leaves takes the
                    # show's key); an episode points at its parent.
                    "seriesId": (r.get("ratingKey") if typ == "show"
                                 else r.get("seriesId")),
                    # Already used to build the label; returned too so the UI
                    # can offer "this season" beside "the whole show".
                    "season": r.get("season"),
                    "series": (r.get("title") if typ == "show"
                               else r.get("grandparentTitle"))})
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


def _log_peek(j: dict) -> dict:
    """Enough of the log to draw a collapsed strip, without sending the log.

    The dashboard folds the log away by default and shows the newest line
    plus a count. Both are O(1) against a list that can hold thousands of
    entries, which is the whole reason log=0 exists."""
    lines = j.get("log") or []
    return {"logLines": len(lines), "lastLine": lines[-1] if lines else ""}


@app.post("/api/jobs/{job_id}/stop")
def api_stop_job(job_id: int):
    """Ask a running or queued job to stop.

    Cooperative, not a kill: the worker notices between titles, and inside a
    long pass at the next progress marker. Work already written to disk
    stays. A job that has already finished is left alone rather than
    reporting a stop that did nothing."""
    for j in _jobs:
        if j["id"] == job_id:
            if j["status"] in ("done", "failed", "stopped"):
                return {"status": j["status"], "stopping": False}
            j["cancel"] = True
            if j["status"] == "queued":
                # Never started, so nothing will reach a marker to notice.
                with _lock:
                    if j in _queue:
                        _queue.remove(j)
                j["status"] = "stopped"
                return {"status": "stopped", "stopping": False}
            return {"status": j["status"], "stopping": True}
    raise HTTPException(404, "no such job")


@app.get("/api/jobs")
def api_jobs(id: int | None = None, log: int = 1):
    if id is not None:
        for j in _jobs:
            if j["id"] == id:
                # log=0: the dashboard polls a running job for its per-title
                # rows every few seconds and does not want thousands of log
                # lines riding along each time.
                return j if log else {**{kk: v for kk, v in j.items()
                                         if kk != "log"}, **_log_peek(j)}
        raise HTTPException(404, "no such job")
    # `done`/`total` keep the poll cheap: the dashboard draws a progress bar
    # and the live queue from this, and only fetches a full log on request.
    return [{**{kk: j.get(kk) for kk in ("id", "target", "status", "created",
                                         "total", "current", "currentTitle",
                                         "phase", "phaseDone", "phaseTotal",
                                         "phaseFrac", "cancel")},
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
        # Whether the speakers pass can run at all. Offering a pass that would
        # fail is worse than not offering it: the failure arrives as a dead job
        # in the log rather than as an answer to the question being asked.
        "speakers": _speaker_availability(),
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
    #: Fetches the gated pyannote weights for the speakers pass. Needed once,
    #: and not at all if the image was built with them baked in.
    hf_token: str | None = None
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


# --- speaker model weights --------------------------------------------------
#
# DECLARED BEFORE /api/speakers/{content_id}: FastAPI matches in declaration
# order, so with the parameterised route first, "models" would be read as a
# content id and this would 404 on a title that does not exist.


def _speaker_engine_state() -> dict:
    """The engine's own account of whether it can work, or why not."""
    transport = engines.speaker_transport()
    if transport is None:
        return {"reachable": False, "state": "off",
                "message": ("the speakers container is not configured. Start "
                            "it with: docker compose --profile speakers up -d")}
    return transport.model_state()


#: Short-lived cache of the answer above, because /api/setup carries it and
#: /api/setup is called on every screen change. 30s: long enough that clicking
#: around costs nothing, short enough that starting the container is noticed
#: without a reload. Invalidated outright when a fetch succeeds, so the pass
#: becomes offerable the moment it can actually run.
_SPK_TTL = 30.0
_spk_cache: tuple[float, dict] = (0.0, {})


def _speaker_availability() -> dict:
    """Whether to OFFER the speakers pass at all, cached.

    Note what this does not gate: naming speakers a previous run already found
    is pure orchestrator work on stored clusters, so the labelling screen keeps
    working with the container stopped. Only diarizing needs the engine.
    """
    global _spk_cache
    now = time.time()
    if _spk_cache[1] and now - _spk_cache[0] < _SPK_TTL:
        return _spk_cache[1]
    st = _speaker_engine_state()
    out = {"available": st.get("state") == "ready", "state": st.get("state")}
    _spk_cache = (now, out)
    return out


@app.get("/api/speakers/models")
def api_speaker_models():
    return {**_speaker_engine_state(), "tokenSet": bool(k.hf_token())}


@app.post("/api/speakers/models")
def api_speaker_models_fetch():
    """Download the weights now, using whatever token is configured.

    Synchronous, and it can take minutes. The alternative -- kick off a job
    and poll -- buys nothing here: there is one thing happening, the person who
    clicked is watching it, and a spinner says as much as a progress bar would.
    """
    global _spk_cache
    transport = engines.speaker_transport()
    if transport is None:
        raise HTTPException(409, "the speakers container is not running")
    try:
        out = transport.fetch_models()
        _spk_cache = (0.0, {})     # the pass may have just become offerable
        return out
    except Exception as e:                         # noqa: BLE001 (reported)
        raise HTTPException(
            502, f"engine-speakers did not complete the download: "
                 f"{type(e).__name__}") from e


# --- share actions ----------------------------------------------------------

# --- labelling -----------------------------------------------------------
#
# The only screen in the app where a human supplies data rather than reading
# it. Diarization produces anonymous speakers; these endpoints let someone
# attach names, and turn those names into intervals.


class NameRequest(BaseModel):
    speaker: str
    #: The cast entry being assigned, or None to clear. Chosen from the
    #: title's OWN TMDb cast, so it is an entity with an id rather than free
    #: text that something downstream would have to reconcile.
    actor_id: str | None = None
    character: str | None = None
    #: Set when the name came from a suggestion, carrying the match's cosine.
    #: Absent means a person identified it by ear. That distinction is what
    #: interval confidence is derived from.
    sim: float | None = None


@app.get("/api/speakers/{content_id}")
def api_speakers(content_id: str):
    """Clusters for one title, ranked by dialogue time, with suggestions.

    Ordered longest-first because that is the order worth working in: the
    principals carry most of the runtime, and the tail is mostly one-liners
    that will never clear the audio floor.
    """
    clusters = vpmod.read_clusters(STORE, content_id)
    if not clusters:
        raise HTTPException(404, "no speakers for this title; run the pass")
    path = st.canonical_path(STORE, content_id)
    doc = json.loads(path.read_text()) if path.exists() else {}
    runtime_s = (doc.get("sourceRuntimeMs") or 0) / 1000

    by_actor = {}
    for iv in doc.get("actorIntervals") or []:
        if iv.get("source") == "voice":
            by_actor.setdefault(iv["actorId"], 0)
            by_actor[iv["actorId"]] += 1
    named = _named_speakers(STORE, content_id)

    rows = []
    for s in clusters["speakers"]:
        spk = s["speaker"]
        assigned = named.get(spk)
        row = {"speaker": spk, "seconds": s["seconds"],
               "enrollable": s["enrollable"], "matchable": s["matchable"],
               "assigned": assigned, "suggest": None,
               "spans": _spans(clusters["turns"], spk, runtime_s)}
        if not assigned and s["matchable"] and s.get("embedding"):
            hit = vpmod.suggest(STORE, s["embedding"],
                                exclude_content=content_id)
            if hit:
                hit["confidence"] = vpmod.confidence(hit["sim"])
                hit["explain"] = vpmod.explain(hit["sim"])
            row["suggest"] = hit
        rows.append(row)
    rows.sort(key=lambda r: -r["seconds"])
    return {"contentId": content_id,
            "title": doc.get("title"), "runtime": runtime_s,
            "speechSeconds": round(sum(r["seconds"] for r in rows), 1),
            "cast": doc.get("cast") or [], "rows": rows,
            "enrollMin": vpmod.ENROLL_MIN_S, "matchMin": vpmod.MATCH_MIN_S}


def _spans(turns, speaker: str, runtime_s: float, gap_s: float = 2.0):
    """Merged turns as [start, width] fractions of runtime, for the strip.

    Merged because diarization emits one turn per utterance: a character
    pausing mid-sentence makes two, and unmerged they render as a stipple
    nobody can read. Fractions so the client can draw at any width.
    """
    mine = sorted((s, e) for s, e, k in turns if k == speaker)
    merged = []
    for s, e in mine:
        if merged and s - merged[-1][1] <= gap_s:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    if not runtime_s:
        return []
    return [[round(s / runtime_s, 5), round(max(e - s, 0.4) / runtime_s, 5)]
            for s, e in merged]


def _names_path(store, content_id: str):
    return Path(store) / "speakers" / f"{content_id}.names.json"


def _named_speakers(store, content_id: str) -> dict:
    p = _names_path(store, content_id)
    return json.loads(p.read_text()) if p.exists() else {}


@app.get("/api/speakers/{content_id}/clip/{speaker}")
def api_speaker_clip(content_id: str, speaker: str):
    """One audition file per speaker: several passages stitched together.

    Drawn from WIDELY SEPARATED points in the runtime, not consecutive turns.
    Consecutive ones sound alike even when a cluster merged two characters;
    start/middle/end makes the merge audible as the voice changing partway
    through, which is the only purity check a person can actually perform.
    """
    clusters = vpmod.read_clusters(STORE, content_id)
    if not clusters:
        raise HTTPException(404, "no speakers for this title")
    audio = STORE / "speakers_work" / content_id / f"{content_id}.wav"
    if not audio.exists():
        raise HTTPException(404, "the extracted audio is gone; re-run the pass")

    segs = sorted((s, e) for s, e, k in clusters["turns"]
                  if k == speaker and e - s >= 1.0)
    if not segs:
        raise HTTPException(404, "that speaker has no usable turns")
    n = min(3, len(segs))
    picks = [segs[round(i * (len(segs) - 1) / max(1, n - 1))] for i in range(n)]

    out = STORE / "speakers_work" / content_id / f"clip_{speaker}.wav"
    if not out.exists():
        _stitch(audio, picks, out)
    return FileResponse(out, media_type="audio/wav")


def _stitch(audio: Path, picks, out: Path, each_s: float = 3.0,
            gap_s: float = 0.45) -> None:
    import wave
    with wave.open(str(audio), "rb") as w:
        rate, width, chans = w.getframerate(), w.getsampwidth(), w.getnchannels()
        parts = []
        for i, (s, e) in enumerate(picks):
            if i:
                parts.append(b"\x00" * int(gap_s * rate) * width * chans)
            w.setpos(int(s * rate))
            parts.append(w.readframes(int(min(e - s, each_s) * rate)))
    with wave.open(str(out), "wb") as o:
        o.setnchannels(chans)
        o.setsampwidth(width)
        o.setframerate(rate)
        o.writeframes(b"".join(parts))


@app.post("/api/speakers/{content_id}/name")
def api_name_speaker(content_id: str, req: NameRequest):
    """Assign (or clear) one speaker, then rewrite the title's intervals.

    Rewritten from scratch every time rather than patched: the names file is
    the source of truth, and regenerating is the only way a CLEARED name
    actually removes its intervals.
    """
    clusters = vpmod.read_clusters(STORE, content_id)
    if not clusters:
        raise HTTPException(404, "no speakers for this title")
    names = _named_speakers(STORE, content_id)
    if req.actor_id and req.character:
        names[req.speaker] = {"actorId": req.actor_id,
                              "character": req.character,
                              "sim": req.sim}
        emb = next((s.get("embedding") for s in clusters["speakers"]
                    if s["speaker"] == req.speaker), None)
        enrollable = next((s["enrollable"] for s in clusters["speakers"]
                           if s["speaker"] == req.speaker), False)
        # Enrol only above the floor. A short reference is not dangerous on
        # its own, but it is a poor one, and it would be reused everywhere.
        if enrollable:
            vpmod.enroll(STORE, req.actor_id, actor_id=req.actor_id,
                         character=req.character, embedding=emb,
                         content_id=content_id)
    else:
        names.pop(req.speaker, None)

    np_ = _names_path(STORE, content_id)
    np_.parent.mkdir(parents=True, exist_ok=True)
    np_.write_text(json.dumps(names, indent=1))
    written = _rebuild_intervals(content_id, clusters, names)
    return {"ok": True, "named": len(names), "intervals": written}


def _rebuild_intervals(content_id: str, clusters: dict, names: dict) -> int:
    """Names + turns -> actorIntervals, replacing every voice interval.

    confidence carries the SAME meaning as it does for faces: strength of the
    match to the claimed identity (faces/cluster.py uses mean cosine to the
    reference photo). A suggestion accepted keeps its cosine; a name given by
    ear is 1.0, the top of that scale, because a person listening is stronger
    evidence than any similarity score.
    """
    path = st.canonical_path(STORE, content_id)
    doc = json.loads(path.read_text())
    kept = [iv for iv in (doc.get("actorIntervals") or [])
            if (iv.get("source") or "face") != "voice"]

    fresh = []
    for spk, rec in names.items():
        mine = sorted((s, e) for s, e, k in clusters["turns"] if k == spk)
        merged = []
        for s, e in mine:
            if merged and s - merged[-1][1] <= 2.0:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        conf = round(float(rec["sim"]), 3) if rec.get("sim") else 1.0
        for s, e in merged:
            fresh.append({"actorId": rec["actorId"],
                          "startMs": int(s * 1000), "endMs": int(e * 1000),
                          "confidence": conf, "source": "voice"})

    doc["actorIntervals"] = sorted(kept + fresh,
                                   key=lambda iv: iv.get("startMs") or 0)
    doc.setdefault("provenance", {})["speakers"] = {
        "generated": st.now_iso(), "version": "pyannote-3.1 + human"}
    st.write_timeline(path, doc)
    return len(fresh)


# --- labelling: faces ----------------------------------------------------
#
# Same screen, different sense. Where diarization leaves every speaker
# anonymous, the face pass NAMES the clusters it can and leaves the rest --
# so these rows come in three states, not two: matched against a cast photo,
# suggested from a faceprint learned on another title, or unknown. Matched
# rows are shown too, and are confirmable: a match at the old default put 122
# seconds of one actor's screen time under another actor's name, and only a
# person looking at the face can catch that.


class FaceNameRequest(BaseModel):
    cluster: int
    actor_id: str | None = None
    character: str | None = None
    sim: float | None = None


def _face_names_path(store, content_id: str) -> Path:
    return Path(store) / fpmod.FACE.name / f"{content_id}.names.json"


def _named_faces(store, content_id: str) -> dict:
    return prints.read_json(_face_names_path(store, content_id), {}) or {}


@app.get("/api/faces/{content_id}")
def api_faces(content_id: str):
    """Face clusters for one title, ranked by screen time.

    Below-floor clusters are counted, never listed: on a 59-minute episode
    they were an out-of-focus background face, a cluster that had merged
    three people, and a lamp.
    """
    clusters = fpmod.read_clusters(STORE, content_id)
    if not clusters:
        raise HTTPException(404, "no face clusters for this title; re-index it")
    path = st.canonical_path(STORE, content_id)
    doc = json.loads(path.read_text()) if path.exists() else {}
    named = _named_faces(STORE, content_id)
    cast_by_id = {c["actorId"]: c for c in doc.get("cast") or []}

    rows, below = [], 0
    for c in clusters["clusters"]:
        if not c["nameable"]:
            below += 1
            continue
        key = str(c["cluster"])
        auto = c.get("matched")
        if auto and auto["sim"] < fpmod.MATCH_THRESHOLD:
            auto = None          # under the calibrated bar: not a claim
        row = {
            "cluster": c["cluster"], "seconds": c["screenSeconds"],
            "scenes": c["scenes"], "spans": c["spans"],
            "assigned": named.get(key),
            "matched": (dict(auto, character=(
                cast_by_id.get(auto["actorId"], {}).get("character")
                or cast_by_id.get(auto["actorId"], {}).get("name")
                or auto["actorId"]),
                confidence=fpmod.confidence(auto["sim"]),
                explain=fpmod.explain(auto["sim"], auto.get("via") or ""))
                if auto else None),
            "suggest": None,
        }
        if not row["assigned"] and not auto and c.get("embedding"):
            hit = fpmod.suggest(STORE, c["embedding"],
                                exclude_content=content_id)
            if hit:
                hit["confidence"] = fpmod.confidence(hit["sim"])
                hit["explain"] = fpmod.explain(hit["sim"], "faceprint")
            row["suggest"] = hit
        rows.append(row)
    rows.sort(key=lambda r: -r["seconds"])
    return {"contentId": content_id, "title": doc.get("title"),
            "runtime": (doc.get("sourceRuntimeMs") or 0) / 1000,
            "screenSeconds": round(sum(r["seconds"] for r in rows), 1),
            "cast": doc.get("cast") or [], "rows": rows,
            "belowFloor": below, "minSeconds": fpmod.MIN_SCREEN_S,
            "minScenes": fpmod.MIN_SCENES}


@app.get("/api/faces/{content_id}/crop/{cluster}")
def api_face_crop(content_id: str, cluster: int):
    """The cluster's exemplar montage: three faces from across its life.

    Cut during the pass, because the frames they come from do not outlive it.
    """
    crop = fpmod.crops_dir(STORE, content_id) / f"{cluster}.jpg"
    if not crop.exists():
        raise HTTPException(404, "no crop for that cluster; re-index the title")
    return FileResponse(crop, media_type="image/jpeg")


@app.post("/api/faces/{content_id}/name")
def api_name_face(content_id: str, req: FaceNameRequest):
    """Assign (or clear) one cluster, then rewrite the title's face intervals."""
    clusters = fpmod.read_clusters(STORE, content_id)
    if not clusters:
        raise HTTPException(404, "no face clusters for this title")
    by_id = {c["cluster"]: c for c in clusters["clusters"]}
    if req.cluster not in by_id:
        raise HTTPException(404, f"no cluster {req.cluster} in this title")

    names = _named_faces(STORE, content_id)
    key = str(req.cluster)
    if req.actor_id and req.character:
        names[key] = {"actorId": req.actor_id, "character": req.character,
                      "sim": req.sim}
        cluster = by_id[req.cluster]
        # Enrol only above the floor: a thin reference is not dangerous on
        # its own, but it would be reused on every later title.
        if cluster["nameable"] and cluster.get("embedding"):
            fpmod.enroll(STORE, req.actor_id, actor_id=req.actor_id,
                         character=req.character,
                         embedding=cluster["embedding"],
                         content_id=content_id)
    else:
        names.pop(key, None)

    prints.write_json(_face_names_path(STORE, content_id), names)
    written = _rebuild_face_intervals(content_id, clusters, names)
    return {"ok": True, "named": len(names), "intervals": written,
            # What propagating would reach, so the screen can offer it with a
            # real number instead of a vague "apply elsewhere?"
            "siblings": len(fpmod.siblings(STORE, content_id))}


@app.post("/api/faces/{content_id}/propagate")
def api_propagate_faces(content_id: str):
    """Carry this title's names across the rest of its series.

    Cheap enough to be a button: cluster centroids are already on disk, so
    this is arithmetic, not a re-index. Nothing is fetched and no frame is
    decoded, which is why a season finishes while you watch.
    """
    if not fpmod.series_key(content_id):
        raise HTTPException(422, "only episodes propagate; a film has no "
                                 "siblings to carry names to")
    changed = fpmod.propagate(STORE, content_id)
    episodes = 0
    for cid, _hits in changed.items():
        clusters = fpmod.read_clusters(STORE, cid)
        if clusters:
            _rebuild_face_intervals(cid, clusters, _named_faces(STORE, cid))
            episodes += 1
    return {"ok": True, "episodes": episodes,
            "named": sum(changed.values()),
            "detail": changed}


def _rebuild_face_intervals(content_id: str, clusters: dict,
                            names: dict) -> int:
    """Clusters + names -> the title's face intervals, from scratch.

    Both halves are regenerated -- the automatic matches AND the human ones --
    because the cluster document is the source of truth for what is on
    screen, and rebuilding is the only way a CLEARED name actually loses its
    intervals. It also means the calibrated threshold applies to timelines
    indexed before it existed: a match too weak to believe stops being
    claimed the moment anyone opens the screen.
    """
    path = st.canonical_path(STORE, content_id)
    doc = json.loads(path.read_text())
    kept = [iv for iv in (doc.get("actorIntervals") or [])
            if (iv.get("source") or "face") != "face"]
    runtime_ms = clusters.get("runtimeMs") or doc.get("sourceRuntimeMs") or 0

    fresh = []
    for c in clusters["clusters"]:
        human = names.get(str(c["cluster"]))
        auto = c.get("matched")
        if human:
            actor_id = human["actorId"]
            # A person looking at the face is stronger evidence than any
            # cosine, so a name given by eye is 1.0; an accepted suggestion
            # keeps the score that earned it.
            conf = round(float(human["sim"]), 3) if human.get("sim") else 1.0
        elif auto and auto["sim"] >= fpmod.MATCH_THRESHOLD:
            actor_id, conf = auto["actorId"], auto["sim"]
        else:
            continue
        for start, width in c["spans"]:
            fresh.append({"actorId": actor_id,
                          "startMs": int(start * runtime_ms),
                          "endMs": int((start + width) * runtime_ms),
                          "confidence": conf, "source": "face"})

    doc["actorIntervals"] = sorted(kept + fresh,
                                   key=lambda iv: iv.get("startMs") or 0)
    doc.setdefault("provenance", {})["faces"] = {
        "generated": st.now_iso(),
        "version": f"{clusters.get('version') or 'sface-v1'} + human"}
    st.write_timeline(path, doc)
    return len(fresh)


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

#: The dashboard's CSS/HTML/JS live in service/static/ as REAL FILES, not as
#: string constants in this module. They were constants until 2026-07-29, and
#: that cost us twice: a stray quote inside a Python triple-quote silently
#: broke the entire dashboard script, invisible to every Python tool. As files
#: they get syntax highlighting, `node --check`, and a diff that means
#: something.
#:
#: Still INLINED into one response rather than served as separate assets: the
#: page stays a single request with no cache-busting to get wrong, and
#: `dashboard()` keeps returning the whole document so its tests are unchanged.
_STATIC = Path(__file__).resolve().parent / "static"


def _asset(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


_STYLE = "<style>" + _asset("dashboard.css") + "</style>"


#: The tab icon: the hub's staggered-bar mark with the bars only part
#: written, which is what this side of the project does. Kept as readable SVG
#: and encoded at import rather than pasted in as an opaque blob.
#:
#: Inline because the orchestrator image copies only xray/ and schema/, so a
#: file would need a Dockerfile line and a route to serve it. This also covers
#: the sign-in page, which shares _HEAD.
#:
#: Tracks sit at 48%: below about 40% they vanish at 16px and the mark reads
#: as two ragged bars instead of three partly-filled ones.
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' rx='14' fill='#2f5d55'/>"
    "<rect x='14' y='17' width='20' height='7' rx='3.5' fill='#f7f7f4'/>"
    "<rect x='22' y='29' width='28' height='7' rx='3.5' fill='#f7f7f4'"
    " opacity='.48'/>"
    "<rect x='22' y='29' width='13' height='7' rx='3.5' fill='#f7f7f4'/>"
    "<rect x='14' y='41' width='14' height='7' rx='3.5' fill='#f7f7f4'"
    " opacity='.48'/>"
    "</svg>")

_FAVICON = ('<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,'
            + urllib.parse.quote(_FAVICON_SVG, safe="") + '">')


_HEAD = ('<!doctype html><meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width,initial-scale=1">'
         + _FAVICON)

_LOGIN_PAGE = _HEAD + "<title>OpenXray Generator sign-in</title>" + _STYLE + r"""
<header><div class="brand"><span class="mark"><i></i></span> OpenXray Generator</div></header>
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

_DASH_PAGE = (_HEAD + "<title>OpenXray Generator</title>" + _STYLE
              + _asset("dashboard.html")
              + "<script>" + _asset("dashboard.js")
              + _asset("labelling.js") + "</script>")


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return _LOGIN_PAGE


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return _DASH_PAGE
