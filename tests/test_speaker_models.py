"""Getting the gated pyannote weights, and saying why you cannot.

Three properties carry this feature:

BUILDING WITHOUT A TOKEN MUST WORK. It used to fail, which made a broken
`docker build` the place people first met the HuggingFace gates -- with no UI
and no way to say which of the three was missing.

A RUNTIME-FETCHED ENGINE IS AS READY AS A BAKED ONE. The old readiness check
asked `baked`, so an engine that downloaded its weights would have been
reported unusable forever.

THE DIAGNOSIS MUST BE SPECIFIC. "accept the conditions on one of these three
repos" is the message that build-time could manage. The whole reason for moving
this into the dashboard is being able to say WHICH one.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.fetch_speaker_models as fsm       # noqa: E402
from xray import engines, keys, settings_store as ss  # noqa: E402


def _fake_hub(*, whoami_ok=True, gated=()):
    """A stand-in huggingface_hub. `auth_check` is the real gate probe, and
    `model_info` deliberately is not: it answers 200 unauthenticated for these
    repos, so a fake that let model_info decide would test the wrong thing."""
    hub = mock.MagicMock()
    errors = mock.MagicMock()

    class GatedRepoError(Exception):
        pass

    errors.GatedRepoError = GatedRepoError
    api = mock.MagicMock()

    def whoami(token=None):
        if not whoami_ok:
            raise RuntimeError("401")
        return {"name": "julian"}

    def auth_check(repo, token=None):
        if repo in gated:
            raise GatedRepoError(repo)

    api.whoami.side_effect = whoami
    api.auth_check.side_effect = auth_check
    hub.HfApi.return_value = api
    return {"huggingface_hub": hub, "huggingface_hub.errors": errors}


class TestDiagnosis(unittest.TestCase):

    def _diagnose(self, token, **kw):
        with mock.patch.dict(sys.modules, _fake_hub(**kw)):
            return fsm.diagnose(token)

    def test_no_token_is_its_own_state_not_a_failure(self):
        d = self._diagnose("")
        self.assertEqual(d["state"], "no-token")
        self.assertFalse(d["ok"])

    def test_a_rejected_token_is_told_apart_from_an_unaccepted_gate(self):
        """These need opposite fixes -- make a new token versus click Agree --
        so conflating them sends half of everyone to the wrong page."""
        self.assertEqual(self._diagnose("hf_x", whoami_ok=False)["state"],
                         "bad-token")
        self.assertEqual(self._diagnose("hf_x", gated=fsm.GATED)["state"],
                         "gated")

    def test_the_unaccepted_gate_is_named(self):
        one = ("pyannote/segmentation-3.0",)
        d = self._diagnose("hf_x", gated=one)
        self.assertEqual(d["gated"], list(one))
        self.assertIn("segmentation-3.0", fsm.explain(d))
        self.assertEqual(d["user"], "julian",
                         "the token worked; say so, or it reads as rejected")

    def test_all_gates_accepted_is_ok(self):
        d = self._diagnose("hf_x")
        self.assertTrue(d["ok"])
        self.assertEqual(d["gated"], [])

    def test_community_1_is_checked_because_pyannote_4_needs_it(self):
        """The repo people miss: it is not mentioned by the two obvious ones
        and only appears in pyannote 4.x."""
        self.assertIn("pyannote/speaker-diarization-community-1", fsm.GATED)


class TestBuildWithoutAToken(unittest.TestCase):
    """A plain `docker compose --profile speakers build` has to succeed."""

    def _main(self, env, argv=()):
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch.object(sys, "argv", ["fetch", *argv]), \
             mock.patch.object(fsm, "fetch") as fetched:
            code = fsm.main()
        return code, fetched

    def test_no_token_skips_softly_and_downloads_nothing(self):
        code, fetched = self._main({"HF_TOKEN": ""})
        self.assertEqual(code, 0, "a missing token must not fail the build")
        fetched.assert_not_called()

    def test_a_token_that_does_not_work_does_fail_the_build(self):
        """Asking for a baked image and silently not getting one is worse than
        a red build: nothing downstream would notice until first use."""
        with mock.patch.dict("os.environ", {"HF_TOKEN": "hf_x"}), \
             mock.patch.object(sys, "argv", ["fetch"]), \
             mock.patch.object(fsm, "fetch",
                               return_value={"ok": False, "state": "gated",
                                             "gated": list(fsm.GATED)}):
            self.assertEqual(fsm.main(), 1)

    def test_diagnose_mode_prints_json_for_the_engine_to_read(self):
        with mock.patch.dict("os.environ", {"HF_TOKEN": ""}), \
             mock.patch.object(sys, "argv", ["fetch", "--diagnose"]), \
             mock.patch("builtins.print") as p:
            self.assertEqual(fsm.main(), 0)
        self.assertEqual(json.loads(p.call_args[0][0])["state"], "no-token")

    def test_the_script_still_parses_under_the_container_python(self):
        """It is run by path from a subprocess, so an import-time error would
        surface as an unexplained non-zero exit rather than a traceback."""
        r = subprocess.run([sys.executable, "-m", "py_compile",
                            str(Path(fsm.__file__))], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr.decode())


class TestEngineStateWithoutTheNetwork(unittest.TestCase):
    """/health is polled, so it reports what is on disk and never asks
    HuggingFace anything. The network questions belong to POST /models."""

    def setUp(self):
        from xray.service import engine_speakers as es
        self.es = es
        self.d = tempfile.TemporaryDirectory()
        self.baked = Path(self.d.name) / "opt"
        self.runtime = Path(self.d.name) / "vol"
        es.BAKED_CACHE = str(self.baked)
        es.RUNTIME_CACHE = str(self.runtime)
        es._last_fetch = {}
        es._fetching = False

    def tearDown(self):
        self.d.cleanup()

    def _weights(self, where):
        where.mkdir(parents=True, exist_ok=True)
        (where / "model.bin").write_bytes(b"w")

    def test_an_empty_cache_directory_is_not_ready(self):
        """HF_HOME gets created by anything that touches the library, so
        existence would report a fresh volume as ready."""
        self.runtime.mkdir(parents=True)
        self.assertFalse(self.es._has_weights(str(self.runtime)))

    def test_weights_anywhere_under_the_cache_count(self):
        self._weights(self.runtime / "hub" / "models--pyannote" / "snap")
        self.assertTrue(self.es._has_weights(str(self.runtime)))

    def test_the_baked_cache_wins_when_it_has_weights(self):
        """An operator who baked asked for a container that reaches nothing."""
        self._weights(self.baked)
        self._weights(self.runtime)
        self.assertEqual(self.es._cache(), str(self.baked))

    def test_no_token_and_no_weights_asks_for_a_token(self):
        with mock.patch.object(self.es.k, "hf_token", return_value=""):
            self.assertEqual(self.es._state()["state"], "no-token")

    def test_a_token_with_no_weights_asks_for_a_download(self):
        with mock.patch.object(self.es.k, "hf_token", return_value="hf_x"):
            self.assertEqual(self.es._state()["state"], "needs-fetch")

    def test_weights_present_is_ready_even_with_no_token(self):
        """The token is for fetching. Once fetched it is not needed, and a
        state that kept demanding it would be a permanent false alarm."""
        self._weights(self.runtime)
        with mock.patch.object(self.es.k, "hf_token", return_value=""):
            st = self.es._state()
        self.assertEqual(st["state"], "ready")
        self.assertFalse(st["baked"])

    def test_a_failed_attempt_outranks_needing_one(self):
        """"download the weights" is useless advice to someone whose download
        just failed on an unaccepted gate."""
        self.es._last_fetch = {"ok": False, "state": "gated",
                               "gated": ["pyannote/segmentation-3.0"]}
        with mock.patch.object(self.es.k, "hf_token", return_value="hf_x"):
            st = self.es._state()
        self.assertEqual(st["state"], "gated")
        self.assertEqual(st["gated"], ["pyannote/segmentation-3.0"])

    def test_health_never_reaches_huggingface(self):
        """Guard on the polling path: an import of huggingface_hub here would
        make every dashboard render do network I/O."""
        with mock.patch.object(self.es.k, "hf_token", return_value="hf_x"), \
             mock.patch.dict(sys.modules, {"huggingface_hub": None}):
            self.assertTrue(self.es.health()["ok"])


class TestFetchClearsTheStaleVerdict(unittest.TestCase):
    """The bug this is here to prevent: weights arrive, and the pass keeps
    reporting the load error from before they existed."""

    def setUp(self):
        from xray.service import engine_speakers as es
        self.es = es
        self.d = tempfile.TemporaryDirectory()
        self.runtime = Path(self.d.name) / "vol"
        es.RUNTIME_CACHE = str(self.runtime)
        es.BAKED_CACHE = str(Path(self.d.name) / "nothing")
        es._load_error = "RuntimeError: no usable weights"
        es._pipeline = "a stale pipeline object"
        es._fetching = False
        es._last_fetch = {}

    def tearDown(self):
        self.d.cleanup()
        self.es._load_error = ""
        self.es._pipeline = None

    def test_a_successful_fetch_lets_the_model_load_again(self):
        def fake_run(cmd, **kw):
            self.runtime.mkdir(parents=True, exist_ok=True)
            (self.runtime / "model.bin").write_bytes(b"w")
            return mock.Mock(returncode=0, stdout="baked 96 MB", stderr="")

        with mock.patch.object(self.es.k, "hf_token", return_value="hf_x"), \
             mock.patch.object(subprocess, "run", side_effect=fake_run):
            out = self.es.fetch_models()

        self.assertTrue(out["ok"])
        self.assertEqual(out["state"], "ready")
        self.assertEqual(self.es._load_error, "",
                         "the old verdict is stale once weights exist")
        self.assertIsNone(self.es._pipeline, "and the pipeline must reload")

    def test_the_child_is_the_only_thing_allowed_online(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(kw.get("env") or {})
            return mock.Mock(returncode=1, stdout="", stderr="nope")

        self.es._load_error = ""
        with mock.patch.object(self.es.k, "hf_token", return_value="hf_x"), \
             mock.patch.dict("os.environ", {"HF_HUB_OFFLINE": "1"}), \
             mock.patch.object(subprocess, "run", side_effect=fake_run):
            self.es.fetch_models()

        self.assertNotIn("HF_HUB_OFFLINE", seen,
                         "the fetch child must not inherit the offline pin")
        self.assertEqual(seen.get("HF_HOME"), str(self.runtime),
                         "and must never write into the baked cache")

    def test_a_failed_fetch_does_not_leave_the_service_marked_busy(self):
        with mock.patch.object(self.es.k, "hf_token", return_value="hf_x"), \
             mock.patch.object(subprocess, "run",
                               side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.es.fetch_models()
        self.assertFalse(self.es._fetching,
                         "a stuck flag would 409 every later attempt")


class TestReadinessTalksAboutTheDashboard(unittest.TestCase):
    """The transport turns an engine state into something a person can act on,
    and the action is now a screen rather than a docker command."""

    def _ready(self, state):
        t = engines.HttpSpeakerEngine("http://engine-speakers:8083")
        with mock.patch.object(t, "model_state",
                               return_value={"reachable": True, **state}):
            return t.ready()

    def test_a_runtime_fetched_engine_is_ready(self):
        """The regression guard. readiness used to require `baked`, so an
        engine that downloaded its own weights read as broken forever."""
        ok, why = self._ready({"state": "ready", "baked": False})
        self.assertTrue(ok, why)

    def test_a_baked_engine_is_still_ready(self):
        ok, _ = self._ready({"state": "ready", "baked": True})
        self.assertTrue(ok)

    def test_every_unready_state_says_where_to_go(self):
        for state in ("no-token", "needs-fetch", "bad-token", "gated",
                      "load-failed"):
            ok, why = self._ready({"state": state})
            self.assertFalse(ok, state)
            self.assertIn("Speakers", why,
                          f"{state} must name the screen that fixes it")

    def test_an_unknown_future_state_still_refuses_clearly(self):
        ok, why = self._ready({"state": "something-new"})
        self.assertFalse(ok)
        self.assertIn("something-new", why)

    def test_an_unreachable_engine_is_a_state_not_an_exception(self):
        t = engines.HttpSpeakerEngine("http://nowhere:8083")
        with mock.patch("requests.get", side_effect=OSError("refused")):
            st = t.model_state()
        self.assertFalse(st["reachable"])
        ok, why = t.ready()
        self.assertFalse(ok)
        self.assertIn("--profile speakers up -d", why)
        self.assertNotIn("--build", why,
                         "building is no longer part of the happy path")


class TestThePassIsOnlyOfferedWhenItCanRun(unittest.TestCase):
    """The container is opt-in and its weights are a separate download, so on
    most installs this pass cannot run. Offering it anyway moves the refusal
    from the moment of asking into a job log twenty seconds later."""

    def setUp(self):
        import os
        with mock.patch.dict("os.environ", {"XRAY_STORE": os.getcwd()}):
            from xray.service import orchestrator as O
        self.O = O
        O._spk_cache = (0.0, {})

    def tearDown(self):
        self.O._spk_cache = (0.0, {})

    def _avail(self, transport):
        with mock.patch.object(self.O.engines, "speaker_transport",
                              return_value=transport):
            return self.O._speaker_availability()

    def test_no_container_means_not_offered(self):
        got = self._avail(None)
        self.assertFalse(got["available"])
        self.assertEqual(got["state"], "off")

    def test_a_container_with_no_weights_is_not_offered(self):
        t = mock.Mock()
        t.model_state.return_value = {"reachable": True, "state": "no-token"}
        self.assertFalse(self._avail(t)["available"])

    def test_a_ready_container_is_offered(self):
        t = mock.Mock()
        t.model_state.return_value = {"reachable": True, "state": "ready"}
        self.assertTrue(self._avail(t)["available"])

    def test_the_answer_is_cached_so_setup_stays_cheap(self):
        """/api/setup carries this and is called on every screen change; an
        uncached probe would put a network round trip on each one."""
        t = mock.Mock()
        t.model_state.return_value = {"reachable": True, "state": "ready"}
        self._avail(t)
        self._avail(t)
        self.assertEqual(t.model_state.call_count, 1)

    def test_a_successful_fetch_invalidates_the_cache(self):
        """Otherwise starting the container leaves the pass unofferable for up
        to the TTL, which reads as the download not having worked."""
        t = mock.Mock()
        t.model_state.return_value = {"reachable": True, "state": "no-token"}
        self._avail(t)
        t.fetch_models.return_value = {"ok": True, "state": "ready"}
        with mock.patch.object(self.O.engines, "speaker_transport",
                              return_value=t):
            self.O.api_speaker_models_fetch()
        t.model_state.return_value = {"reachable": True, "state": "ready"}
        self.assertTrue(self._avail(t)["available"])

    def test_naming_does_not_need_the_container(self):
        """Clusters are already on disk, so labelling is orchestrator work.
        Gating it on the engine would strand anyone who stopped the container
        after diarizing -- with the owed work still shown but unfinishable."""
        import inspect
        src = inspect.getsource(self.O.api_speakers)
        self.assertNotIn("speaker_transport", src)
        self.assertNotIn("engines", src)


def _write_wav(path, *, seconds=1.0, rate=16000, channels=1, width=2):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x00\x01" * int(rate * seconds) * channels
                      if width == 2 else
                      b"\x00" * int(rate * seconds) * channels)


class _FakeAnnotation:
    def labels(self):
        return ["SPEAKER_00"]

    def itertracks(self, yield_label=False):
        class Seg:
            start, end = 0.0, 1.0
        return iter([(Seg(), None, "SPEAKER_00")])


class TestAudioReachesPyannoteWithoutTorchcodec(unittest.TestCase):
    """pyannote 4.x decodes through torchcodec, whose default wheel is
    CUDA-linked and cannot load in a CPU-only image (libnvrtc.so.13). Every
    diarize died with "torchcodec is not available" after the audio pull had
    already run. Handing over a waveform is pyannote's own alternative and
    takes that whole ABI chain out of the hot path."""

    def setUp(self):
        from xray.service import engine_speakers as es
        self.es = es
        self.d = tempfile.TemporaryDirectory()
        self.tmp = Path(self.d.name)

    def tearDown(self):
        self.d.cleanup()
        self.es._pipeline = None

    def test_a_mono_16k_wav_is_read_as_one_channel(self):
        p = self.tmp / "a.wav"
        _write_wav(p, seconds=0.5)
        samples, rate = self.es._wav_frames(p)
        self.assertEqual(rate, 16000)
        self.assertEqual(samples.shape, (1, 8000))

    def test_stereo_is_transposed_to_channel_time(self):
        """pyannote wants (channel, time); WAV stores samples interleaved, so
        a reshape without the transpose would interleave the two channels into
        one and call the result twice as long."""
        p = self.tmp / "s.wav"
        _write_wav(p, seconds=0.5, channels=2)
        samples, _ = self.es._wav_frames(p)
        self.assertEqual(samples.shape, (2, 8000))

    def test_samples_are_scaled_to_the_unit_range(self):
        p = self.tmp / "a.wav"
        _write_wav(p, seconds=0.1)
        samples, _ = self.es._wav_frames(p)
        self.assertLessEqual(abs(samples).max(), 1.0)

    def test_something_that_is_not_a_wav_falls_through(self):
        p = self.tmp / "x.mp3"
        p.write_bytes(b"not a wav at all")
        self.assertIsNone(self.es._wav_frames(p))

    def test_8_bit_audio_falls_through_rather_than_being_misread(self):
        """Reading 8-bit samples as int16 would halve the duration and garble
        every one of them, which diarization would happily analyse."""
        p = self.tmp / "b.wav"
        _write_wav(p, seconds=0.5, width=1)
        self.assertIsNone(self.es._wav_frames(p))

    def test_diarize_hands_the_pipeline_a_waveform_not_a_path(self):
        """The wiring, mocked past torch: what matters is that a path never
        reaches the pipeline, because that is the call that needs torchcodec."""
        p = self.tmp / "a.wav"
        _write_wav(p, seconds=0.5)
        loaded = {"waveform": "TENSOR", "sample_rate": 16000}
        seen = {}

        def fake_pipeline(source, **kw):
            seen["source"] = source
            return _FakeAnnotation()

        self.es._pipeline = fake_pipeline
        with mock.patch.object(self.es, "_read_wav", return_value=loaded):
            out = self.es.diarize(self.es.DiarizeRequest(audio_path=str(p)))
        self.assertIs(seen["source"], loaded,
                      "a path would be decoded by torchcodec and fail")
        self.assertEqual(out["labels"], ["SPEAKER_00"])

    def test_an_unreadable_file_still_reaches_the_pipeline_as_a_path(self):
        """The fallback must stay real: a non-WAV is pyannote's problem to
        decode, not something to refuse outright."""
        p = self.tmp / "x.mp3"
        p.write_bytes(b"not a wav")
        seen = {}

        def fake_pipeline(source, **kw):
            seen["source"] = source
            return _FakeAnnotation()

        self.es._pipeline = fake_pipeline
        self.es.diarize(self.es.DiarizeRequest(audio_path=str(p)))
        self.assertEqual(seen["source"], str(p))


class TestATruncatedPullIsNotSilent(unittest.TestCase):
    """ffmpeg exits 0 on an HTTP input that closes early, leaving a partial
    file. Every interval written from it would be CORRECT, so nothing
    downstream could tell that the last third of the film was never examined --
    and the timeline would be published as complete."""

    def setUp(self):
        from xray import frames
        self.frames = frames
        self.d = tempfile.TemporaryDirectory()
        self.p = Path(self.d.name) / "a.wav"

    def tearDown(self):
        self.d.cleanup()

    def test_a_short_extract_raises(self):
        _write_wav(self.p, seconds=60)            # got 1 minute
        with self.assertRaises(RuntimeError) as e:
            self.frames._check_full_length(self.p, 180_000)   # wanted 3
        self.assertIn("short", str(e.exception))
        self.assertIn("33%", str(e.exception),
                      "say how much arrived, not just that it was wrong")

    def test_a_full_extract_passes(self):
        _write_wav(self.p, seconds=60)
        self.frames._check_full_length(self.p, 60_000)

    def test_a_second_of_slack_is_tolerated(self):
        """Container audio and video streams legitimately differ slightly, and
        a trailing-silence trim is normal."""
        _write_wav(self.p, seconds=60)
        self.frames._check_full_length(self.p, 60_800)

    def test_an_unmeasurable_file_is_not_treated_as_truncated(self):
        self.p.write_bytes(b"not a wav")
        self.frames._check_full_length(self.p, 60_000)

    def test_a_network_input_gets_reconnect_options_before_the_input(self):
        """They configure the protocol, so after -i they are silently ignored
        and the drop this exists to survive happens anyway."""
        opts = self.frames._input_opts("https://plex.example.com/library/x")
        self.assertIn("-reconnect_streamed", opts,
                      "a Plex part URL is not seekable; plain -reconnect "
                      "does not cover it")
        self.assertIn("-reconnect", opts)

    def test_a_local_file_gets_none_of_them(self):
        """ffmpeg warns about unused options on every call otherwise."""
        self.assertEqual(self.frames._input_opts("/media/film.mkv"), [])


class TestTheEngineErrorSurvivesTheHop(unittest.TestCase):
    """A bare "500 Server Error" meant the only way to learn anything was
    `docker logs`, after a run that had already spent half an hour on audio."""

    def _diarize_against(self, status, payload, text=""):
        t = engines.HttpSpeakerEngine("http://engine-speakers:8083")
        resp = mock.Mock(ok=False, status_code=status, text=text)
        resp.json.side_effect = (lambda: payload) if payload is not None \
            else ValueError("no json")
        with mock.patch("requests.post", return_value=resp):
            with self.assertRaises(RuntimeError) as e:
                t.diarize(Path("/x.wav"))
        return str(e.exception)

    def test_the_engines_reason_reaches_the_job_log(self):
        msg = self._diarize_against(
            500, {"detail": "torchcodec is not available"})
        self.assertIn("torchcodec is not available", msg)
        self.assertIn("500", msg)

    def test_a_non_json_body_still_says_something(self):
        msg = self._diarize_against(502, None, text="<html>bad gateway</html>")
        self.assertIn("502", msg)
        self.assertIn("bad gateway", msg)


class TestRouteOrderingFootgun(unittest.TestCase):
    """/api/speakers/models sits under the same prefix as
    /api/speakers/{content_id}, and FastAPI matches in declaration order. Get
    that wrong and "models" is read as a title id: the endpoint 404s with a
    message about an unknown title, which is a genuinely baffling way for a
    setup screen to fail."""

    def test_models_is_declared_before_the_content_id_route(self):
        import os
        with mock.patch.dict("os.environ", {"XRAY_STORE": os.getcwd()}):
            from xray.service.orchestrator import app
        paths = [r.path for r in app.routes
                 if getattr(r, "path", "").startswith("/api/speakers")]
        self.assertIn("/api/speakers/models", paths)
        self.assertLess(paths.index("/api/speakers/models"),
                        min(i for i, p in enumerate(paths)
                            if "{content_id}" in p))


class TestTheTokenIsAManagedCredential(unittest.TestCase):
    """It goes where every other credential goes: settings.json on the store
    volume, chmod 0600, redacted on read-out."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.p = Path(self.d.name) / "settings.json"
        self.env = mock.patch.dict(
            "os.environ", {"XRAY_SETTINGS": str(self.p)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.d.cleanup()

    def test_it_round_trips_through_the_settings_api(self):
        ss.update({"hf_token": "hf_secret"})
        self.assertEqual(keys.hf_token(), "hf_secret")

    def test_it_is_never_returned_in_the_clear(self):
        ss.update({"hf_token": "hf_secret"})
        self.assertNotIn("hf_secret", json.dumps(ss.redacted()))

    def test_the_file_stays_private(self):
        ss.update({"hf_token": "hf_secret"})
        self.assertEqual(self.p.stat().st_mode & 0o777, 0o600)

    def test_an_env_var_seeds_it_on_first_boot(self):
        """So an existing .env keeps working after this change."""
        with mock.patch.dict("os.environ", {"HF_TOKEN": "hf_from_env"}):
            ss.ensure_seeded()
        self.assertEqual(ss.get("hf_token"), "hf_from_env")


if __name__ == "__main__":
    unittest.main()
