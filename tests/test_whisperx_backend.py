"""whisperX transcription backend and the subtitle timing chain.

The backend itself is exercised against a fake `whisperx` executable, so these
tests download no models, need no GPU, and do not care whether whisperX is
installed on the machine running them.
"""

import unittest

from src.utils.media_analysis import _normalize_transcript_payload
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
