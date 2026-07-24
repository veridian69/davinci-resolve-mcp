"""whisperX transcription backend and the subtitle timing chain.

The backend itself is exercised against a fake `whisperx` executable, so these
tests download no models, need no GPU, and do not care whether whisperX is
installed on the machine running them.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

from src.utils.media_analysis import (
    WHISPERX_BOOLEAN_FLAGS,
    WHISPERX_STR2BOOL_FLAGS,
    WHISPERX_VALUE_FLAGS,
    detect_capabilities,
    _normalize_transcript_payload,
    _transcribe,
    _transcribe_with_whisperx,
    _whisperx_argv,
    _whisperx_env,
)
from src.utils.subtitles import group_segments_by_speaker
from src.utils.subtitle_timing import (
    RetimeNotSupported,
    source_seconds_to_timeline_frame,
    timeline_frame_to_srt_seconds,
)


class SpeakerLabelTests(unittest.TestCase):
    """Diarization is worthless if the speaker label does not survive parsing."""

    def test_segment_speaker_label_survives_normalization(self):
        raw = {
            "language": "es",
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "hola",
                    "speaker": "SPEAKER_00",
                }
            ],
        }

        payload = _normalize_transcript_payload(raw, "whisperx", "es")

        self.assertEqual(payload["segments"][0].get("speaker"), "SPEAKER_00")

    def test_word_speaker_label_survives_normalization(self):
        """Words carry their own label; a word can differ from its segment."""
        raw = {
            "language": "es",
            "segments": [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "text": "hola que tal",
                    "speaker": "SPEAKER_00",
                    "words": [
                        {"word": "hola", "start": 0.0, "end": 0.5,
                         "speaker": "SPEAKER_00"},
                        {"word": "que", "start": 0.5, "end": 1.0,
                         "speaker": "SPEAKER_00"},
                        {"word": "tal", "start": 1.0, "end": 2.0,
                         "speaker": "SPEAKER_01"},
                    ],
                }
            ],
        }

        payload = _normalize_transcript_payload(raw, "whisperx", "es")

        labels = [word.get("speaker") for word in payload["words"]]
        self.assertEqual(labels, ["SPEAKER_00", "SPEAKER_00", "SPEAKER_01"])


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

    def test_defaults_target_cpu_because_ctranslate2_has_no_metal_backend(self):
        argv = _whisperx_argv("whisperx", "/media/take01.mov", "/work", {})

        # whisperx defaults to --device cuda --compute_type float16. Neither
        # exists on Apple Silicon: CTranslate2 ships no Metal backend, and
        # float16 is not a CPU compute type.
        self.assertEqual(_flag_value(argv, "--device"), "cpu")
        self.assertEqual(_flag_value(argv, "--compute_type"), "int8")

    def test_caller_can_override_the_device(self):
        """The defaults are a floor, not a cage -- a CUDA box should work."""
        argv = _whisperx_argv(
            "whisperx", "/media/take01.mov", "/work",
            {"device": "cuda", "compute_type": "float16"},
        )

        self.assertEqual(_flag_value(argv, "--device"), "cuda")
        self.assertEqual(_flag_value(argv, "--compute_type"), "float16")

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


FAKE_WHISPERX = '''#!{python}
"""Stands in for the real whisperx: no models, no torch, no GPU."""
import json, os, sys

argv = sys.argv[1:]
audio = argv[0]
out_dir = argv[argv.index("--output_dir") + 1]
os.makedirs(out_dir, exist_ok=True)

# Record what we were called with so the test can assert on it. Deliberately
# NOT in out_dir: the backend globs that directory for its result, and a
# stray file there would force production code to know about this fixture.
with open(os.environ["FAKE_WHISPERX_RECORD"], "w") as fh:
    json.dump({{"argv": argv, "hf_token_env": os.environ.get("HF_TOKEN")}}, fh)

stem = os.path.splitext(os.path.basename(audio))[0]
with open(os.path.join(out_dir, stem + ".json"), "w") as fh:
    json.dump({{
        "language": "es",
        "segments": [
            {{"start": 0.0, "end": 1.0, "text": "hola", "speaker": "SPEAKER_00",
             "words": [{{"word": "hola", "start": 0.0, "end": 1.0,
                        "speaker": "SPEAKER_00"}}]}},
            {{"start": 1.0, "end": 2.0, "text": "que tal", "speaker": "SPEAKER_01",
             "words": [{{"word": "que", "start": 1.0, "end": 1.5,
                        "speaker": "SPEAKER_01"}},
                       {{"word": "tal", "start": 1.5, "end": 2.0,
                        "speaker": "SPEAKER_01"}}]}},
        ],
    }}, fh)
'''


class WhisperxBackendTests(unittest.TestCase):
    """End to end through the backend, against a fake executable."""

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

        # _whisperx_env copies os.environ, so this reaches the fake.
        self.record = os.path.join(self.tmp, "called_with.json")
        patcher = unittest.mock.patch.dict(
            os.environ, {"FAKE_WHISPERX_RECORD": self.record})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_diarized_transcript_comes_back_with_speakers(self):
        result = _transcribe_with_whisperx(
            self.audio, self.artifacts,
            {"executable": self.fake, "diarize": True},
        )

        self.assertTrue(result.get("success"), result)
        self.assertEqual(result["backend"], "whisperx")
        self.assertEqual(
            [segment.get("speaker") for segment in result["segments"]],
            ["SPEAKER_00", "SPEAKER_01"],
        )

    def test_token_reaches_the_subprocess_environment_not_its_argv(self):
        secret = "hf_thisIsNotARealTokenJustAFixture"

        _transcribe_with_whisperx(
            self.audio, self.artifacts,
            {"executable": self.fake, "diarize": True, "hf_token": secret},
        )

        with open(self.record, encoding="utf-8") as fh:
            called = json.load(fh)

        self.assertEqual(called["hf_token_env"], secret)
        self.assertFalse(any(secret in arg for arg in called["argv"]))


class DispatchTests(WhisperxBackendTests):
    """A backend has to be reachable through _transcribe, not just exist.

    _run_backend returns "fallthrough" for anything it does not recognise and
    an outer block decides what that means, so a backend wired into only one of
    the two is advertised and dead.
    """

    def _transcribe_via_dispatch(self, **overrides):
        transcription = {
            "enabled": True,
            "backend": "whisperx",
            "executable": self.fake,
            "allow_model_download": True,
        }
        transcription.update(overrides)
        return _transcribe(
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

    def test_model_download_gate_applies_to_whisperx_too(self):
        """whisperx downloads ASR, alignment and diarization weights."""
        result = self._transcribe_via_dispatch(allow_model_download=False)

        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("status"), "skipped")


class CapabilityDetectionTests(WhisperxBackendTests):
    """WHISPERX_BIN is how a venv-installed whisperx becomes discoverable."""

    def test_whisperx_bin_makes_the_backend_available(self):
        with unittest.mock.patch.dict(os.environ,
                                      {"WHISPERX_BIN": self.fake}):
            capabilities = detect_capabilities()

        transcription = capabilities["transcription"]
        self.assertIn("whisperx", transcription["backends"])
        self.assertTrue(transcription["available"])

    def test_absent_whisperx_is_not_advertised(self):
        """Advertising a backend that is not installed is how you get a
        silent no-op instead of an install instruction."""
        with unittest.mock.patch.dict(os.environ,
                                      {"WHISPERX_BIN": "/nope/whisperx"}):
            capabilities = detect_capabilities()

        self.assertNotIn("whisperx", capabilities["transcription"]["backends"])


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
