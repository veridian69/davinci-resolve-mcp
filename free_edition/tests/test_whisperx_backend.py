"""whisperX transcription backend, its runtime hooks, and the subtitle timing chain.

The backend itself is exercised against a fake `whisperx` executable, so these
tests download no models, need no GPU, and do not care whether whisperX is
installed on the machine running them.

They also stand in for the boot: upstream's `src/utils/media_analysis.py` is
pristine and knows nothing about whisperX, so everything that used to be an edit
to it is now installed by `free_edition.integrate.register_whisperx()`. Tests
that reach for upstream helpers directly would keep passing against that pristine
file while the hooks production depends on are broken, so the capability,
dispatch and speaker-label cases below deliberately go through the registration.
"""

import ast
import importlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

import free_edition.integrate as integrate
from free_edition.subtitles import whisperx as fe_whisperx
from free_edition.subtitles.srt import (
    SourceWriteRefused,
    audio_extract_argv,
    group_segments_by_speaker,
    split_segments_on_speaker_change,
    write_speaker_srt_files,
)
from free_edition.subtitles.timing import (
    RetimeNotSupported,
    source_seconds_to_timeline_frame,
    timeline_frame_to_srt_seconds,
)
from free_edition.subtitles.whisperx import (
    WHISPERX_BOOLEAN_FLAGS,
    WHISPERX_STR2BOOL_FLAGS,
    WHISPERX_VALUE_FLAGS,
    _transcribe_with_whisperx,
    _whisperx_argv,
    _whisperx_env,
)
from src.utils import media_analysis

# The hooks under test have to be live before any case runs: this call is what
# puts whisperX into upstream's capability report and dispatcher.
#
# Deliberately NOT paired with `from src.utils.media_analysis import
# detect_capabilities, _transcribe`. Those names would bind the ORIGINAL
# functions, captured before this line replaced them -- which is precisely the
# stale-module-level-reference bug this call exists to patch out of four other
# modules, and a test carrying it would report green while production reports
# whisperx absent. Every case below reaches them as `media_analysis.<name>`, an
# attribute lookup that happens at call time.
REGISTRATION = integrate.register_whisperx()

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _flag_value(argv, flag):
    """The argument following `flag`, or None when the flag is absent."""
    if flag not in argv:
        return None
    index = argv.index(flag)
    if index + 1 >= len(argv):
        return None
    return argv[index + 1]


class WhisperxArgvTests(unittest.TestCase):
    """The CLI's own defaults are wrong for every Mac, so the wrapper sets them."""

    def test_device_and_compute_type_are_left_to_whisperx(self):
        """whisperx already picks both:

            --device       default="cuda" if torch.cuda.is_available() else "cpu"
            --compute_type default="default"  # float16 on GPU, float32 on CPU

        Sending our own would override that detection -- forcing "cpu" would
        turn off the GPU on a machine that has one, and forcing int8 would
        quietly trade accuracy the caller never agreed to give up.
        """
        argv = _whisperx_argv("whisperx", "/media/take01.mov", "/work", {})

        self.assertIsNone(_flag_value(argv, "--device"))
        self.assertIsNone(_flag_value(argv, "--compute_type"))

    def test_caller_can_still_pin_device_and_compute_type(self):
        argv = _whisperx_argv(
            "whisperx", "/media/take01.mov", "/work",
            {"device": "cuda", "compute_type": "float16"},
        )

        self.assertEqual(_flag_value(argv, "--device"), "cuda")
        self.assertEqual(_flag_value(argv, "--compute_type"), "float16")

    def test_output_format_defaults_to_json_because_the_backend_parses_it(self):
        argv = _whisperx_argv("whisperx", "/media/take01.mov", "/work", {})

        self.assertEqual(_flag_value(argv, "--output_format"), "json")

    def test_output_format_all_is_allowed_and_still_yields_json(self):
        """`all` writes srt/vtt/txt/tsv/json/aud in one pass, so the caller
        gets subtitle files without a second run and the backend still has
        its JSON to parse."""
        argv = _whisperx_argv("whisperx", "/media/take01.mov", "/work",
                              {"output_format": "all"})

        self.assertEqual(_flag_value(argv, "--output_format"), "all")
        self.assertEqual(argv.count("--output_format"), 1)

    def test_diarization_flags_pass_through(self):
        argv = _whisperx_argv(
            "whisperx", "/media/take01.mov", "/work",
            {"diarize": True, "min_speakers": 2, "max_speakers": 3},
        )

        self.assertIn("--diarize", argv)
        self.assertEqual(_flag_value(argv, "--min_speakers"), "2")
        self.assertEqual(_flag_value(argv, "--max_speakers"), "3")

    def test_str2bool_flags_are_emitted_with_an_explicit_value(self):
        """--highlight_words is `type=str2bool`, not `action="store_true"`.

        Emitted bare it would swallow the next argument as its value, so the
        three categories cannot be collapsed into two.
        """
        argv = _whisperx_argv(
            "whisperx", "/media/take01.mov", "/work",
            {"highlight_words": True},
        )

        self.assertEqual(_flag_value(argv, "--highlight_words"), "True")

    def test_str2bool_flags_can_be_switched_off_explicitly(self):
        """False has to survive: it is a value, not an absence."""
        argv = _whisperx_argv(
            "whisperx", "/media/take01.mov", "/work",
            {"condition_on_previous_text": False},
        )

        self.assertEqual(_flag_value(argv, "--condition_on_previous_text"),
                         "False")

    def test_hugging_face_token_never_reaches_the_command_line(self):
        """A token in argv is readable by any process on the box via `ps`."""
        secret = "hf_thisIsNotARealTokenJustAFixture"

        argv = _whisperx_argv(
            "whisperx", "/media/take01.mov", "/work",
            {"diarize": True, "hf_token": secret},
        )

        self.assertNotIn("--hf_token", argv)
        # Not just the flag: the value must not appear anywhere, including
        # glued to another argument.
        self.assertFalse(
            any(secret in argument for argument in argv),
            "the token leaked into argv",
        )

    def test_hugging_face_token_travels_in_the_environment_instead(self):
        """Paired with the argv test: together they pin it to one channel.

        The argv assertion alone would pass against a function that simply
        drops the token on the floor, which would break diarization silently.
        """
        secret = "hf_thisIsNotARealTokenJustAFixture"

        env = _whisperx_env({"hf_token": secret}, base_env={"PATH": "/usr/bin"})

        self.assertEqual(env.get("HF_TOKEN"), secret)
        self.assertEqual(env.get("PATH"), "/usr/bin")

    def test_absent_token_does_not_clobber_one_already_in_the_environment(self):
        env = _whisperx_env({}, base_env={"HF_TOKEN": "from-the-shell"})

        self.assertEqual(env.get("HF_TOKEN"), "from-the-shell")

    def test_ffmpeg_lib_dir_reaches_the_dynamic_loader(self):
        """torchcodec ships dylibs for FFmpeg 4-7 and refuses to load against
        FFmpeg 8, so a side-by-side FFmpeg 7 has to be findable at runtime."""
        env = _whisperx_env(
            {}, base_env={"WHISPERX_FFMPEG_LIB": "/opt/ffmpeg@7/lib"})

        self.assertIn("/opt/ffmpeg@7/lib", env.get("DYLD_LIBRARY_PATH", ""))

    def test_existing_dyld_path_is_prepended_to_not_replaced(self):
        env = _whisperx_env({}, base_env={
            "WHISPERX_FFMPEG_LIB": "/opt/ffmpeg@7/lib",
            "DYLD_LIBRARY_PATH": "/already/here",
        })

        self.assertEqual(env["DYLD_LIBRARY_PATH"],
                         "/opt/ffmpeg@7/lib:/already/here")

    def test_no_ffmpeg_lib_anywhere_leaves_the_loader_alone(self):
        """Candidates are patched out: otherwise this passes or fails
        depending on whether the machine running it happens to have
        ffmpeg@7, which is not a property of the code.

        The patch target follows the constant: `_detect_ffmpeg_lib_dir` reads it
        as a module global of free_edition.subtitles.whisperx, so patching the
        upstream copy (which no longer has one) would silently do nothing.
        """
        with unittest.mock.patch.object(
                fe_whisperx, "WHISPERX_FFMPEG_LIB_CANDIDATES", ()):
            env = _whisperx_env({}, base_env={})

        self.assertNotIn("DYLD_LIBRARY_PATH", env)

    def test_a_candidate_holding_only_ffmpeg8_libs_is_rejected(self):
        """Homebrew leaves ffmpeg@6 and ffmpeg@7 pointing at whatever ffmpeg
        is current after an upgrade, so the directory can exist and hold the
        one libavutil torchcodec cannot use. Existence is not enough."""
        with tempfile.TemporaryDirectory() as fake_lib:
            open(os.path.join(fake_lib, "libavutil.60.dylib"), "w").close()
            with unittest.mock.patch.object(
                    fe_whisperx, "WHISPERX_FFMPEG_LIB_CANDIDATES",
                    (fake_lib,)):
                env = _whisperx_env({}, base_env={})

        self.assertNotIn("DYLD_LIBRARY_PATH", env)

    def test_a_candidate_holding_a_usable_libavutil_is_accepted(self):
        with tempfile.TemporaryDirectory() as fake_lib:
            open(os.path.join(fake_lib, "libavutil.59.dylib"), "w").close()
            with unittest.mock.patch.object(
                    fe_whisperx, "WHISPERX_FFMPEG_LIB_CANDIDATES",
                    (fake_lib,)):
                env = _whisperx_env({}, base_env={})

        self.assertEqual(env.get("DYLD_LIBRARY_PATH"), fake_lib)


FAKE_WHISPERX = '''#!{python}
"""Stands in for the real whisperx: no models, no torch, no GPU."""
import json, os, shutil, sys

argv = sys.argv[1:]
audio = argv[0]
out_dir = argv[argv.index("--output_dir") + 1]
os.makedirs(out_dir, exist_ok=True)

# Record what we were called with so the test can assert on it. Deliberately
# NOT in out_dir: the backend globs that directory for its result, and a
# stray file there would force production code to know about this fixture.
with open(os.environ["FAKE_WHISPERX_RECORD"], "w") as fh:
    json.dump({{"argv": argv, "hf_token_env": os.environ.get("HF_TOKEN")}}, fh)

# The payload is supplied by the test rather than baked in here, so a case can
# ask for an undiarized transcript or a top-level word list without a second fake.
stem = os.path.splitext(os.path.basename(audio))[0]
shutil.copyfile(os.environ["FAKE_WHISPERX_PAYLOAD"],
                os.path.join(out_dir, stem + ".json"))
'''


# Two speakers, and one word whose own label differs from the label of the
# segment it sits in. That word is the point: whisperx sets a segment's speaker
# by majority overlap, so a post-pass that simply copies each segment's label
# down onto its words would satisfy every other assertion here while quietly
# flattening the conversation into one track.
DIARIZED_PAYLOAD = {
    "language": "es",
    "segments": [
        {"start": 0.0, "end": 1.0, "text": "hola", "speaker": "SPEAKER_00",
         "words": [{"word": "hola", "start": 0.0, "end": 1.0,
                    "speaker": "SPEAKER_00"}]},
        {"start": 1.0, "end": 2.0, "text": "que tal", "speaker": "SPEAKER_01",
         "words": [{"word": "que", "start": 1.0, "end": 1.5,
                    "speaker": "SPEAKER_01"},
                   {"word": "tal", "start": 1.5, "end": 2.0,
                    "speaker": "SPEAKER_00"}]},
    ],
}


class FakeWhisperxCase(unittest.TestCase):
    """Fixture only: a fake whisperx on disk and somewhere for it to write.

    Holds no assertions so that subclassing it costs nothing -- the classes
    below inherit the fixture, not a second copy of somebody else's cases.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.fake = os.path.join(self.tmp, "fake_whisperx")
        with open(self.fake, "w", encoding="utf-8") as fh:
            fh.write(FAKE_WHISPERX.format(python=sys.executable))
        os.chmod(self.fake, 0o755)

        self.audio = os.path.join(self.tmp, "take01.wav")
        with open(self.audio, "wb") as fh:
            fh.write(b"not really audio")

        self.artifacts = {
            "analysis_json": os.path.join(self.tmp, "out", "analysis.json"),
            "transcript_json": os.path.join(self.tmp, "out", "transcript.json"),
            "transcript_srt": os.path.join(self.tmp, "out", "transcript.srt"),
        }

        # _whisperx_env copies os.environ, so these reach the fake.
        self.record = os.path.join(self.tmp, "called_with.json")
        self.payload_path = os.path.join(self.tmp, "payload.json")
        self._set_payload(DIARIZED_PAYLOAD)
        patcher = unittest.mock.patch.dict(os.environ, {
            "FAKE_WHISPERX_RECORD": self.record,
            "FAKE_WHISPERX_PAYLOAD": self.payload_path,
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _set_payload(self, payload):
        """What the fake whisperx will emit as its raw JSON output."""
        with open(self.payload_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def _transcribe(self, **overrides):
        transcription = {"executable": self.fake, "diarize": True}
        transcription.update(overrides)
        return _transcribe_with_whisperx(self.audio, self.artifacts,
                                         transcription)


class WhisperxBackendTests(FakeWhisperxCase):
    """End to end through the backend, against a fake executable."""

    def test_diarized_transcript_comes_back_with_speakers(self):
        result = self._transcribe()

        self.assertTrue(result.get("success"), result)
        self.assertEqual(result["backend"], "whisperx")
        self.assertEqual(
            [segment.get("speaker") for segment in result["segments"]],
            ["SPEAKER_00", "SPEAKER_01"],
        )

    def test_token_reaches_the_subprocess_environment_not_its_argv(self):
        secret = "hf_thisIsNotARealTokenJustAFixture"

        self._transcribe(hf_token=secret)

        with open(self.record, encoding="utf-8") as fh:
            called = json.load(fh)

        self.assertEqual(called["hf_token_env"], secret)
        self.assertFalse(any(secret in arg for arg in called["argv"]))

    def test_a_missing_executable_is_a_skip_not_a_crash(self):
        result = self._transcribe(executable="/nope/whisperx")

        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("status"), "skipped")


class SpeakerLabelSurvivalTests(FakeWhisperxCase):
    """The labels have to come out the far end of upstream's normalizer.

    Upstream's `_normalize_transcript_payload` builds each segment and word dict
    from a fixed key list that does not include `speaker`, so every label is
    dropped there. free_edition's backend re-attaches them by zipping the raw
    payload against the normalized one. Get that pass wrong and transcription
    still succeeds, the transcript still looks right, and subtitle import
    silently produces a single "Unassigned" track.
    """

    def test_segment_speaker_label_survives_normalization(self):
        result = self._transcribe()

        self.assertEqual(
            [segment.get("speaker") for segment in result["segments"]],
            ["SPEAKER_00", "SPEAKER_01"])

    def test_word_speaker_label_survives_normalization(self):
        """Words carry their own label; a word can differ from its segment."""
        result = self._transcribe()

        self.assertEqual([word.get("speaker") for word in result["words"]],
                         ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"])

    def test_a_word_keeps_its_own_label_inside_its_segment(self):
        """Same check one level down: `words` is flattened across segments, so
        the flat list alone would not catch a pass that reassembled it."""
        result = self._transcribe()

        second = result["segments"][1]
        self.assertEqual([word.get("speaker") for word in second["words"]],
                         ["SPEAKER_01", "SPEAKER_00"])

    def test_top_level_words_keep_their_labels_too(self):
        """A raw payload can carry one flat `words` array instead of per-segment
        ones. Upstream normalizes that separately and REPLACES the words it
        collected from the segments, so it needs its own re-attach pass -- the
        per-segment one never touches those dicts."""
        self._set_payload({
            "language": "es",
            "segments": [{"start": 0.0, "end": 2.0, "text": "hola tal",
                          "speaker": "SPEAKER_00"}],
            "words": [
                {"word": "hola", "start": 0.0, "end": 1.0,
                 "speaker": "SPEAKER_00"},
                {"word": "tal", "start": 1.0, "end": 2.0,
                 "speaker": "SPEAKER_01"},
            ],
        })

        result = self._transcribe()

        self.assertEqual([word.get("speaker") for word in result["words"]],
                         ["SPEAKER_00", "SPEAKER_01"])

    def test_an_undiarized_transcript_gains_no_speaker_keys(self):
        """Run without --diarize there is no label to re-attach, and inventing
        an empty one would give every cue a speaker named "" and split a
        one-voice transcript across a track nobody asked for."""
        self._set_payload({
            "language": "es",
            "segments": [{"start": 0.0, "end": 1.0, "text": "hola",
                          "words": [{"word": "hola", "start": 0.0,
                                     "end": 1.0}]}],
        })

        result = self._transcribe(diarize=False)

        self.assertNotIn("speaker", result["segments"][0])
        self.assertNotIn("speaker", result["words"][0])


class DispatchTests(FakeWhisperxCase):
    """A backend has to be reachable through _transcribe, not just exist.

    _run_backend returns "fallthrough" for anything it does not recognise and
    an outer block decides what that means, so a backend wired into only one of
    the two is advertised and dead. Both of those live inside a closure in
    upstream's pristine _transcribe, which is why register_whisperx() replaces
    the whole function rather than patching a set -- and why this reaches it as
    `media_analysis._transcribe`, resolved after the replacement, rather than
    through a name imported before it.
    """

    def _transcribe_via_dispatch(self, **overrides):
        transcription = {
            "enabled": True,
            "backend": "whisperx",
            "executable": self.fake,
            "allow_model_download": True,
        }
        transcription.update(overrides)
        return media_analysis._transcribe(
            self.audio,
            self.artifacts,
            {"transcription": transcription},
            {"transcription": {"available": True, "backends": ["whisperx"]}},
        )

    def test_whisperx_is_reachable_through_the_dispatcher(self):
        result = self._transcribe_via_dispatch()

        self.assertNotEqual(result.get("status"), "fallthrough")
        self.assertNotEqual(result.get("status"), "not_implemented")
        self.assertTrue(result.get("success"), result)
        self.assertEqual(result["backend"], "whisperx")

    def test_whisperx_is_used_when_the_caller_names_no_backend(self):
        """The replacement dispatcher has to resolve the default the way
        upstream does -- capabilities["transcription"]["backends"][0] -- or
        putting whisperx at the head of that list buys nothing."""
        result = self._transcribe_via_dispatch(backend=None)

        self.assertTrue(result.get("success"), result)
        self.assertEqual(result["backend"], "whisperx")

    def test_model_download_gate_applies_to_whisperx_too(self):
        """whisperx downloads ASR, alignment and diarization weights."""
        result = self._transcribe_via_dispatch(allow_model_download=False)

        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("status"), "skipped")

    def test_another_backend_still_goes_to_upstream_untouched(self):
        """The wrapper delegates anything that is not whisperx. If it swallowed
        the call instead, every non-whisperx transcription in the product would
        break the moment the free edition booted."""
        result = self._transcribe_via_dispatch(backend="mock")

        self.assertNotEqual(result.get("backend"), "whisperx")


class CapabilityDetectionTests(FakeWhisperxCase):
    """WHISPERX_BIN is how a venv-installed whisperx becomes discoverable.

    Read through `media_analysis.detect_capabilities` rather than an imported
    name: register_whisperx() replaces the module attribute, and a name bound
    before that call would still point at upstream's original, which never
    reports whisperx at all.
    """

    def test_whisperx_bin_makes_the_backend_available(self):
        with unittest.mock.patch.dict(os.environ,
                                      {"WHISPERX_BIN": self.fake}):
            capabilities = media_analysis.detect_capabilities()

        transcription = capabilities["transcription"]
        self.assertIn("whisperx", transcription["backends"])
        self.assertTrue(transcription["available"])

    def test_whisperx_leads_the_backend_list_rather_than_trailing_it(self):
        """_transcribe takes backends[0] when the caller names no backend, so
        appending instead of inserting at the head is a silent regression:
        transcripts keep appearing, they just lose their speaker labels and
        every voice collapses onto one subtitle track."""
        with unittest.mock.patch.dict(os.environ,
                                      {"WHISPERX_BIN": self.fake}):
            capabilities = media_analysis.detect_capabilities()

        self.assertEqual(capabilities["transcription"]["backends"][0],
                         "whisperx")

    def test_absent_whisperx_is_not_advertised(self):
        """Advertising a backend that is not installed is how you get a
        silent no-op instead of an install instruction."""
        with unittest.mock.patch.dict(os.environ,
                                      {"WHISPERX_BIN": "/nope/whisperx"}):
            capabilities = media_analysis.detect_capabilities()

        self.assertNotIn("whisperx",
                         capabilities["transcription"]["backends"])

    def test_an_absent_whisperx_reports_how_to_install_it(self):
        """The tools table is what the dashboard reads to turn "not available"
        into an actionable instruction."""
        with unittest.mock.patch.dict(os.environ,
                                      {"WHISPERX_BIN": "/nope/whisperx"}):
            capabilities = media_analysis.detect_capabilities()

        entry = capabilities["tools"]["whisperx"]
        self.assertFalse(entry["available"])
        self.assertTrue(entry.get("install"))

    def test_the_rest_of_the_capability_report_is_left_alone(self):
        """The wrapper post-processes upstream's return value; it must not
        rebuild it. A missing key here breaks callers that never asked about
        transcription at all."""
        capabilities = media_analysis.detect_capabilities()

        for key in ("platform", "tools", "transcription"):
            self.assertIn(key, capabilities)


class ToolInstallRegistryTests(unittest.TestCase):
    """register_whisperx() adds the install plan upstream no longer carries."""

    def test_whisperx_has_an_install_plan(self):
        self.assertIn("whisperx", media_analysis.TOOL_INSTALL)

    def test_the_plan_resolves_to_a_command_a_user_can_run(self):
        """install_plan_for() reads TOOL_INSTALL at call time, so a plain dict
        insertion is enough -- but only if the entry has the shape it expects.
        An unknown tool comes back with command=None instead of raising."""
        plan = media_analysis.install_plan_for("whisperx",
                                               platform_id="macos_apple_silicon")

        self.assertEqual(plan["tool"], "whisperx")
        self.assertTrue(plan.get("command"))


def _captures_detect_capabilities(path):
    """True when `path` binds detect_capabilities at module level.

    Function-local imports are excluded deliberately: those resolve at call
    time and pick the wrapper up without help, which is why upstream's own
    internal call sites need nothing done to them.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue
        if (isinstance(node, ast.ImportFrom)
                and (node.module or "").endswith("media_analysis")
                and any(alias.name == "detect_capabilities"
                        for alias in node.names)):
            return True
        # Module level includes a try/except or an `if` around the import;
        # only a function or class body changes when it is resolved.
        pending.extend(ast.iter_child_nodes(node))
    return False


class RuntimeRegistrationTests(unittest.TestCase):
    """What the boot asserts on, asserted offline.

    Every failure mode here is silent in production: nothing raises, the server
    starts, and transcription quietly runs on a backend that cannot diarize.
    """

    # Modules that bind detect_capabilities with a MODULE-LEVEL from-import.
    # Each holds the original function object, so patching the attribute on
    # media_analysis alone leaves them calling the unwrapped version --
    # whisperx reported absent, no error anywhere. src/server.py is the one
    # every survey of this delta missed, and it is the compound console's path.
    CAPTORS = (
        "src/analysis_dashboard.py",
        "src/batch_cli.py",
        "src/server.py",
        "src/utils/media_analysis_jobs.py",
    )

    def test_registration_reports_the_hooks_it_installed(self):
        if not isinstance(REGISTRATION, dict):
            self.skipTest(
                "register_whisperx() returned no report on this call; it is "
                "idempotent and something registered before this module")
        for key in ("tool_install", "detect_capabilities", "transcribe"):
            self.assertTrue(REGISTRATION.get(key),
                            f"{key} hook not reported as installed")

    def test_the_set_of_modules_capturing_detect_capabilities_has_not_grown(self):
        """A static scan, because the runtime one cannot see this: a module
        imported after registration binds the wrapper for free, so a fifth
        captor would look fine in-process and break only in a real boot, where
        upstream is fully imported before integrate runs. An upstream merge
        that adds one has to be noticed here."""
        found = sorted(
            str(path.relative_to(REPO_ROOT))
            for path in sorted((REPO_ROOT / "src").rglob("*.py"))
            if _captures_detect_capabilities(path)
        )

        self.assertEqual(
            found, sorted(self.CAPTORS),
            "the module-level captors of detect_capabilities changed; "
            "free_edition.integrate.register_whisperx() rebinds a fixed list "
            "and anything missing from it silently reports whisperx absent")

    def test_every_importable_captor_sees_the_wrapper(self):
        """Weaker than the scan above -- a module first imported here picks the
        wrapper up on its own -- but it is what catches the wrapper never being
        installed at all, and it costs nothing.

        Checks every name a captor could hold it under, not just the canonical
        one. `src/server.py:76` imports it as `detect_capabilities as
        detect_media_analysis_capabilities`, so on a module imported AFTER
        registration -- which is what happens here, and never in a real boot --
        the canonical name is simply absent. Asserting on that name alone fails
        against correct code, which is what this test did before.
        """
        # Every name in src/ that any module binds the function under.
        NAMES = ("detect_capabilities", "detect_media_analysis_capabilities")

        checked = {}
        for dotted in ("src.utils.media_analysis_jobs", "src.batch_cli",
                       "src.analysis_dashboard", "src.server"):
            try:
                module = importlib.import_module(dotted)
            except ImportError:
                continue  # an optional runtime dependency is absent offline
            bound = {name: getattr(module, name, None) for name in NAMES}
            present = {name: value for name, value in bound.items()
                       if value is not None}
            if not present:
                # Binds it under neither name: it is not a captor at all, so
                # there is nothing here that could go stale.
                continue
            checked[dotted] = all(value is media_analysis.detect_capabilities
                                  for value in present.values())

        if not checked:
            self.skipTest("no captor module is importable in this environment")
        self.assertEqual(
            [name for name, ok in checked.items() if not ok], [],
            "a captor is holding a stale detect_capabilities under at least "
            "one of its bound names")

    def test_registering_twice_does_not_nest_the_wrappers(self):
        """The boot line gets re-pasted after every edit, and each re-paste
        calls this again. A wrapper that wrapped itself would advertise
        whisperx once per paste and eventually recurse."""
        before = media_analysis.detect_capabilities
        integrate.register_whisperx()
        self.assertIs(media_analysis.detect_capabilities, before)

        # sys.executable stands in for the binary: the resolver only checks
        # that the path exists, and this test is about how many times the
        # entry is added, not about what it points at.
        with unittest.mock.patch.dict(os.environ,
                                      {"WHISPERX_BIN": sys.executable}):
            backends = media_analysis.detect_capabilities()["transcription"]["backends"]

        self.assertEqual(backends.count("whisperx"), 1, backends)


class InstalledCliDriftTests(unittest.TestCase):
    """Guards the flag list against the CLI it claims to wrap.

    The flag tables were first written from whisperX's main branch while the
    installed version was older, and shipped a --hotwords that the installed
    CLI rejected outright. Only a real binary can catch that, so this test
    skips when there is none rather than pretending to.
    """

    def setUp(self):
        self.executable = (os.environ.get("WHISPERX_BIN")
                           or shutil.which("whisperx"))
        if not self.executable or not os.path.exists(self.executable):
            self.skipTest("whisperx not installed")

    def test_every_advertised_flag_exists_in_the_installed_cli(self):
        help_text = subprocess.run(
            [self.executable, "--help"],
            capture_output=True, text=True, timeout=120,
        ).stdout

        unknown = [
            name for name in (WHISPERX_VALUE_FLAGS + WHISPERX_BOOLEAN_FLAGS
                         + WHISPERX_STR2BOOL_FLAGS)
            if f"--{name}" not in help_text
        ]

        self.assertEqual(
            unknown, [],
            f"advertised but absent from the installed whisperx: {unknown}",
        )


class SpeakerSplitTests(unittest.TestCase):
    """whisperx writes one SRT with every speaker mixed; a track per voice
    needs them separated, and that separation is ours, not the CLI's."""

    SEGMENTS = [
        {"start": 0.0, "end": 1.0, "text": "hola", "speaker": "SPEAKER_00"},
        {"start": 1.0, "end": 2.0, "text": "que tal", "speaker": "SPEAKER_01"},
        {"start": 2.0, "end": 3.0, "text": "bien", "speaker": "SPEAKER_00"},
    ]

    def test_segments_group_under_their_speaker(self):
        groups = group_segments_by_speaker(self.SEGMENTS)

        self.assertEqual(list(groups), ["SPEAKER_00", "SPEAKER_01"])
        self.assertEqual([s["text"] for s in groups["SPEAKER_00"]],
                         ["hola", "bien"])
        self.assertEqual([s["text"] for s in groups["SPEAKER_01"]],
                         ["que tal"])

    def test_speaker_order_follows_first_appearance(self):
        """Track 1 should be whoever speaks first, not whoever sorts first."""
        segments = [
            {"start": 0.0, "end": 1.0, "text": "b", "speaker": "SPEAKER_09"},
            {"start": 1.0, "end": 2.0, "text": "a", "speaker": "SPEAKER_00"},
        ]

        self.assertEqual(list(group_segments_by_speaker(segments)),
                         ["SPEAKER_09", "SPEAKER_00"])

    def test_undiarized_transcript_yields_a_single_unnamed_group(self):
        """Without --diarize there is no label, and one track is correct."""
        segments = [{"start": 0.0, "end": 1.0, "text": "hola"}]

        groups = group_segments_by_speaker(segments)

        self.assertEqual(len(groups), 1)
        self.assertEqual(len(next(iter(groups.values()))), 1)


class AudioExtractTests(unittest.TestCase):
    """Transcribing the used range instead of the whole clip.

    whisperx decodes internally, so extracting a whole file first is pure
    waste. Extracting a *range* is not: eight seconds of a two-hour clip is
    the difference between usable and unusable on CPU.
    """

    def test_only_the_requested_range_is_decoded(self):
        argv = audio_extract_argv("/media/take01.mov", "/work/clip.wav",
                                  start_seconds=12.5, end_seconds=20.5)

        self.assertEqual(_flag_value(argv, "-ss"), "12.5")
        # Duration rather than an absolute end: with -ss applied first, -to
        # would be measured from the seek point on some ffmpeg builds, and -t
        # means the same thing everywhere.
        self.assertEqual(_flag_value(argv, "-t"), "8.0")

    def test_output_is_the_16k_mono_whisper_expects(self):
        argv = audio_extract_argv("/media/take01.mov", "/work/clip.wav",
                                  start_seconds=0.0, end_seconds=1.0)

        self.assertEqual(_flag_value(argv, "-ar"), "16000")
        self.assertEqual(_flag_value(argv, "-ac"), "1")
        self.assertIn("-vn", argv)
        self.assertEqual(argv[-1], "/work/clip.wav")

    def test_refuses_to_write_over_the_source(self):
        """AGENTS.md makes source media read-only. A path bug must not be
        the thing standing between a typo and someone's camera original."""
        with self.assertRaises(SourceWriteRefused):
            audio_extract_argv("/media/take01.mov", "/media/take01.mov",
                               start_seconds=0.0, end_seconds=1.0)

    def test_refuses_to_write_into_the_source_directory(self):
        with self.assertRaises(SourceWriteRefused):
            audio_extract_argv("/media/take01.mov", "/media/take01.wav",
                               start_seconds=0.0, end_seconds=1.0)


class SpeakerHomogeneousSplitTests(unittest.TestCase):
    """A segment's speaker is a majority vote, so it lies on mixed chunks.

    whisperx sets seg['speaker'] to whichever speaker overlapped it longest
    (diarize.py: `max(speaker_intersections.items(), key=...)`). With
    --chunk_size at its default of 30, one segment can hold half a minute of
    four people talking and still carry a single label. The words underneath
    keep their own, correct labels.
    """

    MIXED_SEGMENT = {
        "start": 0.0, "end": 6.0,
        "text": "hola que tal bien gracias",
        "speaker": "SPEAKER_00",           # the majority, and wrong for most of it
        "words": [
            {"word": "hola", "start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"},
            {"word": "que", "start": 1.0, "end": 2.0, "speaker": "SPEAKER_00"},
            {"word": "tal", "start": 2.0, "end": 3.0, "speaker": "SPEAKER_01"},
            {"word": "bien", "start": 3.0, "end": 4.0, "speaker": "SPEAKER_01"},
            {"word": "gracias", "start": 4.0, "end": 6.0, "speaker": "SPEAKER_00"},
        ],
    }

    def test_a_mixed_segment_splits_where_the_speaker_changes(self):
        split = split_segments_on_speaker_change([self.MIXED_SEGMENT])

        self.assertEqual([s["speaker"] for s in split],
                         ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"])
        self.assertEqual([s["text"] for s in split],
                         ["hola que", "tal bien", "gracias"])

    def test_split_segments_carry_the_times_of_their_own_words(self):
        split = split_segments_on_speaker_change([self.MIXED_SEGMENT])

        self.assertEqual((split[1]["start"], split[1]["end"]), (2.0, 4.0))

    def test_a_homogeneous_segment_is_left_alone(self):
        segment = {
            "start": 0.0, "end": 2.0, "text": "hola que", "speaker": "SPEAKER_00",
            "words": [
                {"word": "hola", "start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"},
                {"word": "que", "start": 1.0, "end": 2.0, "speaker": "SPEAKER_00"},
            ],
        }

        split = split_segments_on_speaker_change([segment])

        self.assertEqual(len(split), 1)
        self.assertEqual(split[0]["text"], "hola que")

    def test_segments_without_words_pass_through_untouched(self):
        """No word labels means nothing better to go on than the segment's."""
        segment = {"start": 0.0, "end": 2.0, "text": "hola", "speaker": "S0"}

        split = split_segments_on_speaker_change([segment])

        self.assertEqual(split, [segment])

    def test_grouping_a_mixed_segment_no_longer_collapses_the_conversation(self):
        """The defect this whole class exists for: without the split, a
        thirty-second chunk of four people becomes one speaker's track."""
        groups = group_segments_by_speaker(
            split_segments_on_speaker_change([self.MIXED_SEGMENT]))

        self.assertEqual(sorted(groups), ["SPEAKER_00", "SPEAKER_01"])
        self.assertEqual(len(groups["SPEAKER_00"]), 2)


class SpeakerSrtFileTests(unittest.TestCase):
    """One SRT per voice on disk, which is what Resolve imports."""

    PAYLOAD = {
        "language": "es",
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "hola", "speaker": "SPEAKER_00"},
            {"start": 1.0, "end": 2.0, "text": "que tal", "speaker": "SPEAKER_01"},
            {"start": 2.0, "end": 3.0, "text": "bien", "speaker": "SPEAKER_00"},
        ],
    }

    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.out, True)

    def test_one_file_per_speaker_with_only_that_speakers_cues(self):
        written = write_speaker_srt_files(self.PAYLOAD, self.out)

        self.assertEqual([w["speaker"] for w in written],
                         ["SPEAKER_00", "SPEAKER_01"])
        first = open(written[0]["path"], encoding="utf-8").read()
        self.assertIn("hola", first)
        self.assertIn("bien", first)
        self.assertNotIn("que tal", first)

    def test_cues_are_renumbered_from_one_within_each_file(self):
        """An SRT whose first cue is numbered 3 is not a valid SRT."""
        written = write_speaker_srt_files(self.PAYLOAD, self.out)

        second = open(written[1]["path"], encoding="utf-8").read()
        self.assertTrue(second.lstrip().startswith("1\n"), second[:40])

    def test_a_mixed_chunk_is_split_before_files_are_written(self):
        """End of the chain: a 30s chunk of two people must not land whole on
        one speaker's track just because that speaker talked longest."""
        payload = {"segments": [{
            "start": 0.0, "end": 4.0, "text": "hola tal", "speaker": "SPEAKER_00",
            "words": [
                {"word": "hola", "start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
                {"word": "tal", "start": 2.0, "end": 4.0, "speaker": "SPEAKER_01"},
            ],
        }]}

        written = write_speaker_srt_files(payload, self.out)

        self.assertEqual([w["speaker"] for w in written],
                         ["SPEAKER_00", "SPEAKER_01"])
        self.assertNotIn("tal", open(written[0]["path"], encoding="utf-8").read())

    def test_undiarized_transcript_writes_a_single_file(self):
        payload = {"segments": [{"start": 0.0, "end": 1.0, "text": "hola"}]}

        written = write_speaker_srt_files(payload, self.out)

        self.assertEqual(len(written), 1)
        self.assertTrue(os.path.exists(written[0]["path"]))

    def test_speaker_label_becomes_a_safe_filename(self):
        """Labels reach us from a model, not from a validator."""
        payload = {"segments": [
            {"start": 0.0, "end": 1.0, "text": "x", "speaker": "../../etc/passwd"},
        ]}

        written = write_speaker_srt_files(payload, self.out)

        self.assertEqual(os.path.dirname(os.path.abspath(written[0]["path"])),
                         os.path.abspath(self.out))


class TimelineMappingTests(unittest.TestCase):
    """A word lands where it was spoken, or the subtitles walk off the audio."""

    def test_word_maps_from_source_seconds_to_timeline_frame(self):
        # A clip whose frame 100 of the source sits at frame 500 of the
        # timeline. A word five seconds into the source is at source frame 120,
        # so twenty frames past the clip's head: timeline frame 520.
        frame = source_seconds_to_timeline_frame(
            5.0,
            frame_rate=24.0,
            source_start_frame=100,
            record_start_frame=500,
        )

        self.assertEqual(frame, 520)

    def test_srt_time_is_relative_to_the_timeline_start_timecode(self):
        # Resolve timelines conventionally start at 01:00:00:00, which at 24fps
        # is frame 86400. GetStart() returns absolute frames, but an SRT cue is
        # measured from the head of the programme. Skip this subtraction and
        # every subtitle in the file is an hour late.
        seconds = timeline_frame_to_srt_seconds(
            86400 + 520,
            frame_rate=24.0,
            timeline_start_frame=86400,
        )

        self.assertAlmostEqual(seconds, 520 / 24.0, places=6)


class RetimeTests(unittest.TestCase):
    """A retimed clip breaks the linear mapping; refusing beats drifting."""

    def test_retimed_clip_is_refused_rather_than_mapped(self):
        # At 50% speed a word one minute into the source is two minutes into
        # the timeline. The linear formula would put it a full minute early,
        # and the error grows across the clip rather than staying constant.
        with self.assertRaises(RetimeNotSupported):
            source_seconds_to_timeline_frame(
                60.0,
                frame_rate=24.0,
                source_start_frame=0,
                record_start_frame=0,
                speed_percent=50.0,
            )

    def test_full_speed_clip_is_mapped_normally(self):
        """The guard must not reject the ordinary case it is protecting."""
        frame = source_seconds_to_timeline_frame(
            60.0,
            frame_rate=24.0,
            source_start_frame=0,
            record_start_frame=0,
            speed_percent=100.0,
        )

        self.assertEqual(frame, 1440)


if __name__ == "__main__":
    unittest.main()
