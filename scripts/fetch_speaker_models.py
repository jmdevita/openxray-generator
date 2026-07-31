"""Fetch the gated pyannote diarization weights into a HuggingFace cache.

Runs in two places, and the difference matters:

  BUILD TIME, optionally. `docker build` mounts a token as a BuildKit secret
  and bakes the weights into the image. With no secret this SKIPS and exits 0,
  so a plain build succeeds and produces a usable image -- one that fetches on
  demand instead. It used to fail the build, which made a missing token the
  first thing anyone met.

  RUN TIME, on request. engine-speakers runs this as a SUBPROCESS when someone
  saves a token in the dashboard. A subprocess rather than an in-process call
  because the server sets HF_HUB_OFFLINE before importing huggingface_hub and
  that constant is read at import: letting a child do the fetching is the only
  way to keep the long-lived process permanently offline.

Diagnosis is why this file is more than a download. `from_pretrained` answers
None -- not raises -- for a bad token AND for an unaccepted gate AND for a gate
nobody knew existed, so the failure everyone actually hits is a mysterious
null. `HfApi.auth_check` separates those: whoami proves the token, then one
check per repo names exactly which gate is missing. `model_info` will NOT do,
having checked: it answers 200 unauthenticated for these repos and reports
gated="auto", because the gate blocks file reads, not metadata.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PIPELINE = "pyannote/speaker-diarization-3.1"

#: Every gated repo the pipeline pulls. community-1 appears only in pyannote
#: 4.x and catches people who accepted the two obvious ones, so it is listed
#: rather than discovered one 403 at a time.
GATED = (
    "pyannote/speaker-diarization-3.1",
    "pyannote/segmentation-3.0",
    "pyannote/speaker-diarization-community-1",
)

HF_URL = "https://huggingface.co/"


def diagnose(token: str) -> dict:
    """Why a fetch would fail, established before spending a download on it.

    Returns {"ok", "state", "user", "gated"} where state is one of:
      ok         -- token valid, every gate accepted
      no-token   -- nothing to check with
      bad-token  -- rejected by HuggingFace (revoked, typo, wrong scope)
      gated      -- token fine; `gated` lists the repos still to accept
    """
    token = (token or "").strip()
    if not token:
        return {"ok": False, "state": "no-token", "user": "", "gated": []}

    from huggingface_hub import HfApi
    try:  # moved to .errors in recent versions; .utils has it in older ones
        from huggingface_hub.errors import GatedRepoError
    except ImportError:                        # pragma: no cover - old hub
        from huggingface_hub.utils import GatedRepoError

    api = HfApi()
    try:
        user = (api.whoami(token=token) or {}).get("name", "")
    except Exception:                          # noqa: BLE001 (any = rejected)
        return {"ok": False, "state": "bad-token", "user": "", "gated": []}

    # auth_check asks the question that matters -- may this token read this
    # repo's FILES -- and raises GatedRepoError per repo when it may not.
    blocked = []
    for repo in GATED:
        try:
            api.auth_check(repo, token=token)
        except Exception:                      # noqa: BLE001 (gated, moved or
            blocked.append(repo)               # unreachable: same next step)
    if blocked:
        return {"ok": False, "state": "gated", "user": user, "gated": blocked}
    return {"ok": True, "state": "ok", "user": user, "gated": []}


def fetch(home: str, token: str) -> dict:
    """Download the weights and prove they load. diagnose()-shaped, plus `mb`.

    Instantiating the pipeline is what populates the cache, so this downloads
    AND proves it loads: a failure that would otherwise surface twenty minutes
    into someone's first title surfaces here instead.
    """
    Path(home).mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = home
    os.environ.pop("HF_HUB_OFFLINE", None)     # this is the online path

    d = diagnose(token)
    if not d["ok"]:
        return d

    from pyannote.audio import Pipeline
    pipe = Pipeline.from_pretrained(PIPELINE, token=token)
    if pipe is None:
        # Token works and every gate is accepted, so this is something else --
        # a repo moved, or a pyannote version wanting a fourth one. Saying so
        # beats repeating the accept-the-gates advice, which here would send
        # someone back to pages they have already accepted.
        return {"ok": False, "state": "load-failed", "user": d["user"],
                "gated": []}
    mb = sum(f.stat().st_size for f in Path(home).rglob("*")
             if f.is_file()) / 1e6
    return {"ok": True, "state": "ok", "user": d["user"], "gated": [],
            "mb": round(mb)}


def explain(d: dict) -> str:
    """One human sentence per outcome, shared by the build log and the
    dashboard so both say the same thing about the same state."""
    state = d.get("state")
    if state == "ok":
        return (f"weights ready ({d['mb']} MB)" if d.get("mb")
                else f"token valid ({d.get('user') or 'unknown user'})")
    if state == "no-token":
        return ("no HuggingFace token: the pyannote weights are gated and "
                "cannot be fetched")
    if state == "bad-token":
        return ("HuggingFace rejected that token. Create a read token at "
                + HF_URL + "settings/tokens")
    if state == "gated":
        return ("the token works"
                + (f" ({d['user']})" if d.get("user") else "")
                + ", but these conditions are not accepted yet: "
                + ", ".join(d.get("gated") or []))
    if state == "load-failed":
        return ("the token works and every gate is accepted, but the pipeline "
                "would not load. This is not a permissions problem")
    return f"unexpected state: {state}"


def main() -> int:
    args = sys.argv[1:]
    home = os.environ.get("HF_HOME") or "/opt/hf"
    token = (os.environ.get("HF_TOKEN") or "").strip()

    if "--diagnose" in args:
        print(json.dumps(diagnose(token)))     # the machine-readable path
        return 0

    if not token:
        # A SOFT skip, deliberately. At build time this leaves an image with
        # no weights, which is fine -- it fetches them when someone supplies a
        # token in the dashboard. Failing here would make `docker compose
        # build` the place people first meet the gates, and that is the worst
        # possible place for it: no UI, no per-repo diagnosis, just exit 2.
        print("no HF_TOKEN: skipping the bake. The weights will be fetched "
              "from the dashboard instead (Setup → Speakers).")
        return 0

    d = fetch(home, token)
    print(explain(d), file=sys.stdout if d["ok"] else sys.stderr)
    if not d["ok"] and d.get("gated"):
        print("accept each of:\n  "
              + "\n  ".join(HF_URL + r for r in d["gated"]), file=sys.stderr)
    # A token that WAS supplied and did not work is a real build failure: the
    # operator asked for a baked image and did not get one.
    return 0 if d["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
