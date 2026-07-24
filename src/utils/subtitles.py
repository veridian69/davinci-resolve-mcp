"""Turning a transcript into subtitle files, one per speaker.

whisperX writes a single SRT with every speaker mixed together, so splitting a
diarized transcript into one file per voice is our work, not the CLI's. Kept
apart from `subtitle_timing`, which stays purely numeric.
"""

from collections import OrderedDict

# Used when a transcript carries no diarization labels at all. One unnamed
# group is the honest representation: not "unknown speakers", just no split.
UNLABELLED_SPEAKER = ""


def group_segments_by_speaker(segments):
    """Group transcript segments by their speaker label.

    Returns an OrderedDict keyed by speaker, ordered by first appearance --
    so subtitle track 1 belongs to whoever speaks first, which is what an
    editor expects, rather than to whichever label happens to sort lowest.

    Segments without a label all land in a single group, which is the correct
    shape for a transcript produced without --diarize.
    """
    groups = OrderedDict()
    for segment in segments:
        speaker = segment.get("speaker") or UNLABELLED_SPEAKER
        groups.setdefault(speaker, []).append(segment)
    return groups
