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


class Advance(unittest.TestCase):
    """The within-title position must never retreat.

    Regression: the first cut read `done/total` straight off each event, so
    when the face loop (which counts) handed off to cast matching (which does
    not), the bar fell from full back to zero mid-title.
    """

    def test_a_countable_phase_climbs(self):
        f = 0.0
        for done in (0, 310, 620, 1240):
            nxt = progress.advance(f, {"phase": "faces", "done": done,
                                       "total": 1240})
            self.assertGreaterEqual(nxt, f)
            f = nxt

    def test_uncountable_phases_move_forward_not_backward(self):
        """They own a slice too, so entering one advances rather than resets."""
        f = progress.advance(0.0, {"phase": "faces", "done": 1240,
                                   "total": 1240})
        self.assertGreater(f, 0)
        for phase in ("matching", "writing"):
            f2 = progress.advance(f, {"phase": phase})
            self.assertGreaterEqual(f2, f, f"{phase} moved the bar backwards")
            f = f2

    def test_a_counting_phase_after_another_still_has_room(self):
        """Regression: one high-water mark let extraction fill the whole bar,
        so the face loop had nowhere to go and sat frozen at 95%."""
        after_frames = progress.advance(0.0, {"phase": "frames",
                                              "done": 100, "total": 100})
        mid_faces = progress.advance(after_frames, {"phase": "faces",
                                                    "done": 50, "total": 400})
        end_faces = progress.advance(mid_faces, {"phase": "faces",
                                                 "done": 400, "total": 400})
        self.assertGreater(mid_faces, after_frames)
        self.assertGreater(end_faces, mid_faces)

    def test_an_unknown_phase_holds_rather_than_guessing(self):
        f = progress.advance(0.4, {"phase": "something-new"})
        self.assertEqual(f, 0.4)

    def test_the_bar_never_fills_before_the_title_finishes(self):
        f = progress.advance(0.0, {"phase": "writing"})
        for ev in ({"phase": "writing"}, {"phase": "writing", "done": 1,
                                          "total": 1}):
            f = progress.advance(f, ev)
        self.assertLessEqual(f, progress.WITHIN_TITLE_CAP)
        self.assertLess(f, 1.0)

    def test_the_whole_sequence_is_monotonic(self):
        seq = ([{"phase": "frames"}]
               + [{"phase": "faces", "done": d, "total": 100}
                  for d in range(0, 101, 5)]
               + [{"phase": "matching"}, {"phase": "writing"}])
        f, seen = 0.0, []
        for ev in seq:
            f = progress.advance(f, ev)
            seen.append(f)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[0], 0.0)


class StepRouting(unittest.TestCase):
    """`pipeline.step` is where markers leave the log channel."""

    def _run(self, body, progress_cb):
        """Drive the real `step` closure out of run_title without a backend."""
        logged, result = [], {"steps": {}}

        # Same shape as run_title's inner step(), and built on the REAL sink
        # so this cannot drift back into testing a buffered design that no
        # longer ships. Standing up a media source just to check line routing
        # would exercise everything except the routing.
        def step(name, fn):
            def handle(line):
                event = progress.parse(line)
                if event is not None:
                    if progress_cb:
                        progress_cb({"pass": name, **event})
                    return
                logged.append(f"  [{name}] {line}")

            sink = pipeline._LineSink(handle)
            try:
                with contextlib.redirect_stdout(sink):
                    fn()
                result["steps"][name] = "ok"
            except Exception as e:  # noqa: BLE001
                result["steps"][name] = f"failed: {e}"
            finally:
                sink.close_tail()

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


class Streaming(unittest.TestCase):
    """Lines must reach the handler DURING a pass, not after it returns.

    Regression: the first cut captured into a StringIO and drained it in
    `finally`, so a feature-length index delivered every log line and every
    progress marker in one burst at the end. The dashboard sat on "working…"
    for the whole run, which is exactly the problem progress was added to fix.
    """

    def test_lines_arrive_as_they_are_written(self):
        seen = []
        sink = pipeline._LineSink(seen.append)
        with contextlib.redirect_stdout(sink):
            print("first")
            during = list(seen)          # observed mid-pass
            print("second")
        self.assertEqual(during, ["first"])
        self.assertEqual(seen, ["first", "second"])

    def test_a_line_is_never_split_across_callbacks(self):
        seen = []
        sink = pipeline._LineSink(seen.append)
        sink.write("[progress] pha")
        sink.write("se=faces done=3 total=9\n")
        self.assertEqual(seen, ["[progress] phase=faces done=3 total=9"])
        self.assertEqual(progress.parse(seen[0])["done"], 3)

    def test_trailing_text_without_a_newline_is_flushed(self):
        seen = []
        sink = pipeline._LineSink(seen.append)
        sink.write("no newline here")
        self.assertEqual(seen, [])
        sink.close_tail()
        self.assertEqual(seen, ["no newline here"])


class CancelSignal(unittest.TestCase):
    """Cancel rides the progress callback, so it lands mid-pass."""

    def test_raising_from_the_callback_aborts_the_pass(self):
        emitted = []

        def handle(line):
            event = progress.parse(line)
            if event is None:
                return
            emitted.append(event["done"])
            if event["done"] >= 2:
                raise pipeline.Cancelled("stopped")

        sink = pipeline._LineSink(handle)
        with self.assertRaises(pipeline.Cancelled):
            with contextlib.redirect_stdout(sink):
                for i in range(1, 100):
                    progress.emit("faces", i, 99)

        # Stopped at the third frame, not after all 99.
        self.assertEqual(emitted, [1, 2])

    def test_cancelled_is_not_an_ordinary_failure(self):
        """`step` re-raises it, so a stopped title skips its later passes."""
        self.assertTrue(issubclass(pipeline.Cancelled, Exception))
        self.assertNotIsInstance(pipeline.Cancelled("x"), SystemExit)


class FfmpegProgress(unittest.TestCase):
    """Parsing ffmpeg's -progress stream. The extraction phase is the longest
    part of indexing a feature, and it used to show nothing at all."""

    def test_out_time_us_is_microseconds(self):
        from xray.frames import _progress_ms
        self.assertEqual(_progress_ms("out_time_us=5000000"), 5000)
        self.assertEqual(_progress_ms("out_time_us=0"), 0)

    def test_clock_form_is_the_fallback(self):
        from xray.frames import _progress_ms
        self.assertEqual(_progress_ms("out_time=00:01:23.500000"), 83500)
        self.assertEqual(_progress_ms("out_time=01:00:00.000000"), 3600000)

    def test_out_time_ms_is_ignored_because_it_lies(self):
        """ffmpeg puts MICROseconds in out_time_ms despite the name; trusting
        it would report a 92-minute film as done after 5 seconds."""
        from xray.frames import _progress_ms
        self.assertIsNone(_progress_ms("out_time_ms=5000000"))

    def test_other_keys_and_junk_are_ignored(self):
        from xray.frames import _progress_ms
        for line in ("frame=42", "fps=25.0", "progress=continue", "speed=1.2x",
                     "", "nonsense", "out_time=N/A", "out_time_us=N/A"):
            self.assertIsNone(_progress_ms(line), line)

    def test_negative_positions_clamp_to_zero(self):
        from xray.frames import _progress_ms
        self.assertEqual(_progress_ms("out_time_us=-42"), 0)

    def test_emission_is_throttled_to_whole_percent(self):
        """ffmpeg reports ~twice a second; a feature would otherwise produce
        hundreds of markers for a bar with a hundred positions."""
        total, last, emitted = 92 * 60 * 1000, [-1], []
        for done in range(0, total + 1, 1000):      # one sample per second
            pct = int(100 * done / total)
            if pct != last[0]:
                last[0] = pct
                emitted.append(pct)
        self.assertEqual(len(emitted), 101)
        self.assertEqual(emitted, sorted(emitted))
