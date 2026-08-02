"""What may be deleted, and what is still load-bearing.

The whole module is one judgement call repeated three ways, and getting it
wrong is expensive in both directions: too eager and someone loses the audio
they were halfway through labelling, too shy and a library carries a second
copy of itself in WAV. So these test the HOLD reasons, not just the arithmetic.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xray import retention  # noqa: E402


def write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


class Base(unittest.TestCase):
    def setUp(self):
        self.store = Path(tempfile.mkdtemp())

    def timeline(self, cid, *, provenance=None):
        write(self.store / f"{cid}.json", 0)
        (self.store / f"{cid}.json").write_text(json.dumps(
            {"contentId": cid, "provenance": provenance or {}}))

    def clusters(self, cid, speakers):
        """speakers: [(name, enrollable)]"""
        (self.store / "speakers").mkdir(parents=True, exist_ok=True)
        (self.store / "speakers" / f"{cid}.json").write_text(json.dumps(
            {"contentId": cid,
             "speakers": [{"speaker": s, "seconds": 99.0, "enrollable": e,
                           "matchable": e, "embedding": None}
                          for s, e in speakers],
             "turns": []}))

    def names(self, cid, mapping):
        (self.store / "speakers" / f"{cid}.names.json").write_text(
            json.dumps(mapping))

    def kind(self, report, name):
        for g in report["kinds"]:
            if g["kind"] == name:
                return g
        return None


class TestFrames(Base):
    def test_reclaimable_when_nothing_is_running(self):
        write(self.store / "index_work" / "frames" / "frame_000001.jpg", 500)
        report = retention.survey(self.store)
        self.assertEqual(report["reclaimable"], 500)
        self.assertEqual(self.kind(report, "frames")["bytes"], 500)

    def test_any_running_title_holds_them(self):
        """The frames directory is shared across titles, not per-title, so
        there is no way to tell whose frames are in it."""
        write(self.store / "index_work" / "frames" / "frame_000001.jpg", 500)
        report = retention.survey(self.store, busy=["tmdb-movie-999"])
        self.assertEqual(report["reclaimable"], 0)
        self.assertEqual(report["held"], 500)

    def test_clean_leaves_them_alone_while_busy(self):
        frames = self.store / "index_work" / "frames"
        write(frames / "frame_000001.jpg", 500)
        retention.clean(self.store, busy=["tmdb-movie-999"])
        self.assertTrue(frames.exists())

    def test_drop_frames_is_what_the_pass_calls(self):
        work = self.store / "index_work"
        write(work / "frames" / "frame_000001.jpg", 400)
        write(work / "refs" / "1.jpg", 100)
        self.assertEqual(retention.drop_frames(work), 400)
        self.assertFalse((work / "frames").exists())
        # Reference photos are a cache with its own lifetime; the frames are
        # the 156 MB.
        self.assertTrue((work / "refs" / "1.jpg").exists())

    def test_drop_frames_on_a_pass_that_never_got_there(self):
        self.assertEqual(retention.drop_frames(self.store / "nope"), 0)


class TestMusicAudio(Base):
    def test_held_until_the_music_pass_has_run(self):
        cid = "tmdb-movie-809"
        self.timeline(cid)
        write(self.store / "music_work" / cid / f"{cid}__audio.mp3", 900)
        g = self.kind(retention.survey(self.store), "music")
        self.assertEqual(g["reclaimable"], 0)
        self.assertEqual(g["items"][0]["holding"],
                         "the music pass has not run yet")

    def test_released_once_the_block_is_stamped(self):
        cid = "tmdb-movie-809"
        self.timeline(cid, provenance={"music": {"version": "audd-v1"}})
        write(self.store / "music_work" / cid / f"{cid}__audio.mp3", 900)
        g = self.kind(retention.survey(self.store), "music")
        self.assertEqual(g["reclaimable"], 900)
        self.assertEqual(g["items"], [])

    def test_a_title_with_no_timeline_at_all_is_not_stamped(self):
        """Audio without a timeline means the index died after the pull. The
        music pass cannot have run, so this stays until it does."""
        write(self.store / "music_work" / "tmdb-movie-1" / "a.mp3", 10)
        self.assertEqual(retention.survey(self.store)["reclaimable"], 0)


class TestSpeakerAudio(Base):
    def setUp(self):
        super().setUp()
        self.cid = "tmdb-movie-1175942"
        write(self.store / "speakers_work" / self.cid / f"{self.cid}.wav", 800)

    def test_held_while_a_nameable_speaker_has_no_name(self):
        self.clusters(self.cid, [("SPEAKER_00", True), ("SPEAKER_01", True)])
        self.names(self.cid, {"SPEAKER_00": {"actorId": "1"}})
        g = self.kind(retention.survey(self.store), "speakers")
        self.assertEqual(g["reclaimable"], 0)
        self.assertEqual(g["items"][0]["holding"], "1 speaker still to name")

    def test_plural_reads_correctly(self):
        self.clusters(self.cid, [("SPEAKER_00", True), ("SPEAKER_01", True)])
        g = self.kind(retention.survey(self.store), "speakers")
        self.assertEqual(g["items"][0]["holding"], "2 speakers still to name")

    def test_released_once_every_nameable_one_is_named(self):
        self.clusters(self.cid, [("SPEAKER_00", True), ("SPEAKER_01", False)])
        self.names(self.cid, {"SPEAKER_00": {"actorId": "1"}})
        g = self.kind(retention.survey(self.store), "speakers")
        self.assertEqual(g["reclaimable"], 800)

    def test_nothing_nameable_means_nothing_to_wait_for(self):
        self.clusters(self.cid, [("SPEAKER_00", False)])
        self.assertEqual(retention.survey(self.store)["reclaimable"], 800)

    def test_audio_with_no_clusters_is_orphaned_not_precious(self):
        """The pass died between the pull and the diarize. Nothing can read
        this file, and a re-run pulls it again anyway."""
        self.assertEqual(retention.survey(self.store)["reclaimable"], 800)

    def test_the_title_being_worked_on_is_held(self):
        self.clusters(self.cid, [("SPEAKER_00", False)])
        report = retention.survey(self.store, busy=[self.cid])
        self.assertEqual(report["reclaimable"], 0)


class TestClean(Base):
    def setUp(self):
        super().setUp()
        self.timeline("done", provenance={"music": {}})
        self.timeline("pending")
        write(self.store / "music_work" / "done" / "a.mp3", 100)
        write(self.store / "music_work" / "pending" / "a.mp3", 200)
        write(self.store / "index_work" / "frames" / "f.jpg", 50)

    def test_deletes_exactly_what_the_survey_called_reclaimable(self):
        before = retention.survey(self.store)["reclaimable"]
        result = retention.clean(self.store)
        self.assertEqual(result.freed, before)
        self.assertEqual(result.freed, 150)
        self.assertFalse((self.store / "music_work" / "done").exists())
        self.assertTrue((self.store / "music_work" / "pending").exists())
        self.assertFalse((self.store / "index_work" / "frames").exists())

    def test_dry_run_reports_without_deleting(self):
        result = retention.clean(self.store, dry_run=True)
        self.assertEqual(result.freed, 150)
        self.assertTrue((self.store / "music_work" / "done").exists())
        self.assertTrue((self.store / "index_work" / "frames").exists())

    def test_only_the_kinds_asked_for(self):
        result = retention.clean(self.store, kinds=["frames"])
        self.assertEqual(result.freed, 50)
        self.assertTrue((self.store / "music_work" / "done").exists())

    def test_timelines_are_never_touched(self):
        retention.clean(self.store)
        self.assertTrue((self.store / "done.json").exists())
        self.assertTrue((self.store / "pending.json").exists())

    def test_kept_carries_the_reason_forward(self):
        result = retention.clean(self.store)
        self.assertEqual([c.content_id for c in result.kept], ["pending"])
        self.assertTrue(result.kept[0].holding)


class TestReport(Base):
    def test_the_kinds_add_up_to_the_total(self):
        """The screen shows a stacked bar against `total`. If the parts do not
        sum to the whole, the bar silently under-fills and every proportion on
        it is wrong."""
        self.timeline("t")
        write(self.store / "index_work" / "frames" / "f.jpg", 50)
        write(self.store / "music_work" / "t" / "a.mp3", 100)
        write(self.store / "faces" / "crops" / "t" / "0.jpg", 30)
        write(self.store / "people_cache.json", 70)
        report = retention.survey(self.store)
        self.assertEqual(sum(g["bytes"] for g in report["kinds"]),
                         report["total"])

    def test_empty_kinds_are_left_out(self):
        write(self.store / "index_work" / "frames" / "f.jpg", 50)
        kinds = {g["kind"] for g in retention.survey(self.store)["kinds"]}
        self.assertIn("frames", kinds)
        self.assertNotIn("speakers", kinds)

    def test_an_empty_store_says_nothing_rather_than_crashing(self):
        report = retention.survey(self.store)
        self.assertEqual((report["total"], report["reclaimable"]), (0, 0))
        self.assertEqual(report["kinds"], [])


class TestCleanCommand(Base):
    def parse(self, *argv):
        from xray import cli
        return cli.build_parser().parse_args(
            ["--dir", str(self.store), "clean", *argv])

    def run_cmd(self, *argv):
        import contextlib
        import io
        args = self.parse(*argv)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            args.func(args)
        return buf.getvalue()

    def test_deletes_by_default_and_says_what_went(self):
        write(self.store / "index_work" / "frames" / "f.jpg", 400_000)
        out = self.run_cmd()
        self.assertIn("400 KB", out)
        self.assertIn("freed", out)
        self.assertFalse((self.store / "index_work" / "frames").exists())

    def test_dry_run_says_would(self):
        write(self.store / "index_work" / "frames" / "f.jpg", 400_000)
        out = self.run_cmd("--dry-run")
        self.assertIn("would free", out)
        self.assertTrue((self.store / "index_work" / "frames").exists())

    def test_it_names_what_is_holding_the_rest(self):
        """A total with no reason reads as a bug. The line has to say which
        step would release it."""
        self.timeline("tmdb-movie-809")
        write(self.store / "music_work" / "tmdb-movie-809" / "a.mp3", 900_000)
        out = self.run_cmd("--dry-run")
        self.assertIn("tmdb-movie-809", out)
        self.assertIn("the music pass has not run yet", out)

    def test_an_unknown_kind_stops_rather_than_quietly_doing_nothing(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cmd("--only", "timelines")
        self.assertIn("unknown kind", str(ctx.exception))

    def test_nothing_to_do_is_not_an_error(self):
        self.assertIn("nothing to reclaim", self.run_cmd())


class TestHuman(unittest.TestCase):
    def test_reads_the_way_a_person_would_say_it(self):
        for n, want in ((0, "0 B"), (999, "999 B"), (1500, "1.5 KB"),
                        (156_400_000, "156.4 MB"), (2_000_000, "2 MB"),
                        (2_500_000_000, "2.5 GB"), (4_000_000_000_000, "4000 GB")):
            self.assertEqual(retention.human(n), want, n)


if __name__ == "__main__":
    unittest.main()
