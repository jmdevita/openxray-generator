"""Sub-title progress, carried on the log channel the passes already use.

A pass reports where it is by printing a marker line; `pipeline.step` pulls
those out of the captured stdout and hands them to the job's progress
callback instead of the log. Producer and consumer share this module so the
format cannot drift.

The channel is text on purpose. Passes print, `step` captures, and the
engine-faces service returns text over HTTP — threading a real callback
through all of that would mean changing several signatures and giving the
remote engine a streaming protocol. A marker line composes with every one of
those for free, and can be promoted to a structured callback later without
changing what the dashboard does with it.

Marker lines never reach the job log: a face pass over a feature film emits
hundreds, and the log is something a human reads.
"""
from __future__ import annotations

MARKER = "[progress]"

#: Ordered, with each phase's share of one title's bar. The shares are rough
#: — extraction is network-bound and face detection CPU-bound, so which
#: dominates depends on the machine — but they only decide how the bar is
#: APPORTIONED, never whether it moves. Without them a single high-water mark
#: cannot express two counting phases in a row: extraction would fill the bar
#: and the face loop would then have nowhere left to go.
#:
#: ORDER MUST MATCH EXECUTION. `advance` is monotonic, so a phase emitted out
#: of order parks the bar at the later segment and everything after it holds
#: still.
PHASE_WEIGHTS = (("frames", 0.45), ("faces", 0.42), ("enrolling", 0.06),
                 ("matching", 0.04), ("writing", 0.03))
PHASES = tuple(name for name, _ in PHASE_WEIGHTS)


def _segment(phase: str) -> tuple[float, float] | None:
    """(start, width) of `phase` within a title, or None if unknown."""
    start = 0.0
    for name, weight in PHASE_WEIGHTS:
        if name == phase:
            return start, weight
        start += weight
    return None


def emit(phase: str, done: int = 0, total: int = 0, **extra) -> None:
    """Print one marker line. Cheap enough to call inside a frame loop."""
    parts = [MARKER, f"phase={phase}"]
    if total:
        parts += [f"done={done}", f"total={total}"]
    parts += [f"{k}={v}" for k, v in extra.items()]
    print(" ".join(parts))


def parse(line: str) -> dict | None:
    """A marker line as {phase, done, total, ...}, or None if it isn't one.

    Unknown keys ride through as strings so a pass can add a field without
    this module or the dashboard needing to know about it first.
    """
    line = line.strip()
    if not line.startswith(MARKER):
        return None
    out: dict = {}
    for tok in line[len(MARKER):].split():
        key, sep, val = tok.partition("=")
        if not sep:
            continue
        out[key] = int(val) if val.lstrip("-").isdigit() else val
    return out or None


#: A within-title bar never quite fills. Only the face loop reports a count;
#: cast matching and writing follow it with none, so letting the loop reach
#: 1.0 would park the bar at "finished" while the title was still working.
WITHIN_TITLE_CAP = 0.95


def advance(previous: float, event: dict) -> float:
    """The within-title position after `event`, never going backwards.

    Each phase owns a slice of the bar, so entering one puts the bar at that
    slice's start and counting within it fills only that slice. Two effects
    fall out. A phase with no count (cast matching, writing) still moves the
    bar forward to where it begins, instead of reporting zero and dragging it
    back. And a counting phase that follows another counting phase has room
    left to fill, instead of finding the bar already at its maximum.

    max() with the previous value is a backstop, not the mechanism: markers
    can arrive out of order after a retry, and a bar that retreats is worse
    than one that pauses.
    """
    seg = _segment(str(event.get("phase") or ""))
    if seg is None:
        return previous                 # unknown phase: hold, never guess
    start, width = seg
    return max(previous, (start + width * fraction(event)) * WITHIN_TITLE_CAP)


def fraction(event: dict) -> float:
    """How far through the current phase, 0..1.

    Phases without a total (ffmpeg extraction, clustering) report 0: a bar
    that cannot measure something should not invent a position for it. The
    phase LABEL is what carries the information there.
    """
    total = event.get("total") or 0
    if not isinstance(total, int) or total <= 0:
        return 0.0
    done = event.get("done") or 0
    if not isinstance(done, int):
        return 0.0
    return max(0.0, min(1.0, done / total))
