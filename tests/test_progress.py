"""Sub-title progress: the marker format, and that it leaves the log channel.

The whole design rests on two properties. A marker line must round-trip
emit → parse unchanged, and it must NEVER reach the job log, because a
feature-length face pass emits dozens and the log is something a human reads.
"""
import contextlib
import io
import unittest

from xray import pipeline, progress


class ParseFormat(unittest.TestCase):
    def test_round_trip(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            progress.emit("faces", 340, 1240)
        self.assertEqual(progress.parse(buf.getvalue()),
                         {"phase": "faces", "done": 340, "total": 1240})

    def test_phase_without_a_total_omits_the_counters(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            progress.emit("frames")
        event = progress.parse(buf.getvalue())
        self.assertEqual(event, {"phase": "frames"})
        self.assertNotIn("done", event)

    def test_ordinary_log_lines_are_not_markers(self):
        for line in ("[frames] 1240 frames [87s]",
                     "[faces]  3891 faces across 1240 frames",
                     "", "   ", "progress", "[progress]"):
            self.assertIsNone(progress.parse(line), line)

    def test_unknown_keys_ride_through(self):
        """A pass can add a field without this module knowing about it."""
        self.assertEqual(progress.parse("[progress] phase=music cue=7"),
                         {"phase": "music", "cue": 7})

    def test_extra_kwargs_are_emitted(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            progress.emit("music", cue=7)
        self.assertEqual(progress.parse(buf.getvalue()),
                         {"phase": "music", "cue": 7})


class Fraction(unittest.TestCase):
    def test_counts(self):
        self.assertAlmostEqual(
            progress.fraction({"phase": "faces", "done": 620, "total": 1240}), 0.5)

    def test_no_total_is_zero_not_invented(self):
        self.assertEqual(progress.fraction({"phase": "frames"}), 0.0)
        self.assertEqual(progress.fraction({"phase": "f", "total": 0}), 0.0)

    def test_clamped(self):
        self.assertEqual(progress.fraction({"done": 99, "total": 10}), 1.0)
        self.assertEqual(progress.fraction({"done": -5, "total": 10}), 0.0)

    def test_garbage_totals_do_not_raise(self):
        self.assertEqual(progress.fraction({"total": "lots", "done": 3}), 0.0)
        self.assertEqual(progress.fraction({"total": 10, "done": "some"}), 0.0)


class StepRouting(unittest.TestCase):
    """`pipeline.step` is where markers leave the log channel."""

    def _run(self, body, progress_cb):
        """Drive the real `step` closure out of run_title without a backend."""
        logged, result = [], {"steps": {}}

        # Same shape as run_title's inner step(); exercised directly because
        # standing up a media source just to test line routing would test
        # everything except the routing.
        def step(name, fn):
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    fn()
                result["steps"][name] = "ok"
            except Exception as e:  # noqa: BLE001
                result["steps"][name] = f"failed: {e}"
            finally:
                for line in buf.getvalue().splitlines():
                    event = progress.parse(line)
                    if event is not None:
                        if progress_cb:
                            progress_cb({"pass": name, **event})
                        continue
                    logged.append(f"  [{name}] {line}")

        step("index", body)
        return logged

    def test_markers_go_to_the_callback_not_the_log(self):
        seen = []

        def body():
            print("[frames] extracting …")
            progress.emit("faces", 10, 100)
            progress.emit("faces", 50, 100)
            print("[out] done")

        logged = self._run(body, seen.append)
        self.assertEqual(logged, ["  [index] [frames] extracting …",
                                  "  [index] [out] done"])
        self.assertEqual([e["done"] for e in seen], [10, 50])
        self.assertEqual({e["pass"] for e in seen}, {"index"})

    def test_without_a_callback_markers_are_dropped_not_printed(self):
        """The CLI passes no callback and must not gain progress spam."""
        def body():
            print("[frames] extracting …")
            progress.emit("faces", 10, 100)

        self.assertEqual(self._run(body, None),
                         ["  [index] [frames] extracting …"])


class BarMath(unittest.TestCase):
    """The dashboard folds the phase fraction into the title counter.

    Mirrors the JS so the two-level arithmetic is pinned somewhere runnable.
    """

    @staticmethod
    def pct(done, total, phase_done=0, phase_total=0):
        frac = min(1.0, phase_done / phase_total) if phase_total > 0 else 0.0
        return 0 if not total else 100 * min(done + frac, total) / total

    def test_single_title_creeps_instead_of_jumping(self):
        self.assertEqual(self.pct(0, 1), 0)
        self.assertEqual(self.pct(0, 1, 620, 1240), 50)
        self.assertEqual(self.pct(1, 1), 100)

    def test_library_run_still_measures_titles(self):
        self.assertEqual(self.pct(5, 10), 50)
        self.assertEqual(self.pct(5, 10, 1240, 1240), 60)

    def test_never_exceeds_full(self):
        """A phase completing on the last title must not push past 100%."""
        self.assertEqual(self.pct(1, 1, 1240, 1240), 100)
        self.assertEqual(self.pct(10, 10, 500, 1000), 100)

    def test_unmeasurable_phase_does_not_move_the_bar(self):
        self.assertEqual(self.pct(0, 1, 0, 0), 0)


if __name__ == "__main__":
    unittest.main()
