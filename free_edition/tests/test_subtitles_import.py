"""Planning which subtitle track each speaker's SRT file lands on.

Pure decision logic only: which speaker gets which 1-based subtitle track
index, in what order. The Resolve calls that act on that plan (AddTrack,
SetTrackName, ImportMedia, AppendToTimeline) are exercised against a real
Resolve session by free_edition/tools/live_subtitles_validation.py instead --
the granular package has no mock-based test convention to fit into, and
inventing one here would be its own maintenance burden for a project that
already validates this package live.
"""

import unittest

from free_edition.subtitles.tools import build_subtitle_track_plan


class TrackPlanTests(unittest.TestCase):

    def test_one_track_per_speaker_in_first_appearance_order(self):
        written = [
            {"speaker": "SPEAKER_01", "path": "/out/SPEAKER_01.srt", "cue_count": 38},
            {"speaker": "SPEAKER_00", "path": "/out/SPEAKER_00.srt", "cue_count": 83},
        ]

        plan = build_subtitle_track_plan(written)

        self.assertEqual([p["track_index"] for p in plan], [1, 2])
        self.assertEqual([p["speaker"] for p in plan], ["SPEAKER_01", "SPEAKER_00"])

    def test_track_name_defaults_to_the_speaker_label(self):
        written = [{"speaker": "SPEAKER_00", "path": "/out/x.srt", "cue_count": 1}]

        plan = build_subtitle_track_plan(written)

        self.assertEqual(plan[0]["track_name"], "SPEAKER_00")

    def test_the_unlabelled_group_gets_a_readable_track_name(self):
        """write_speaker_srt_files emits an empty-string speaker key for
        cues no backend could attribute to anyone; naming the track
        "" would be confusing in Resolve's track header."""
        written = [{"speaker": "", "path": "/out/transcript.srt", "cue_count": 22}]

        plan = build_subtitle_track_plan(written)

        self.assertEqual(plan[0]["track_name"], "Unassigned")

    def test_single_speaker_does_not_need_explicit_track_routing(self):
        """Matches what free_edition/tools/probe_subtitle_import.py measured:
        with only one subtitle clip, AppendToTimeline's simple list form is
        enough -- it lands on the one subtitle track that exists. Explicit
        trackIndex is only needed to keep multiple speakers apart."""
        written = [{"speaker": "SPEAKER_00", "path": "/out/x.srt", "cue_count": 4}]

        plan = build_subtitle_track_plan(written)

        self.assertFalse(plan[0]["needs_explicit_track_index"])

    def test_multiple_speakers_need_explicit_track_routing(self):
        written = [
            {"speaker": "SPEAKER_00", "path": "/out/a.srt", "cue_count": 4},
            {"speaker": "SPEAKER_01", "path": "/out/b.srt", "cue_count": 3},
        ]

        plan = build_subtitle_track_plan(written)

        self.assertTrue(all(p["needs_explicit_track_index"] for p in plan))

    def test_empty_input_plans_nothing(self):
        self.assertEqual(build_subtitle_track_plan([]), [])


if __name__ == "__main__":
    unittest.main()
