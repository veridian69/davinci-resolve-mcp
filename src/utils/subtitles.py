"""Turning a transcript into subtitle files, one per speaker.

whisperX writes a single SRT with every speaker mixed together, so splitting a
diarized transcript into one file per voice is our work, not the CLI's. Kept
apart from `subtitle_timing`, which stays purely numeric.
"""

import os
import re

from collections import OrderedDict

# Anything outside this becomes an underscore in a filename. Speaker labels
# come out of a model, not out of a validator, and they end up in a path.
_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

UNDIARIZED_STEM = "transcript"


class SourceWriteRefused(Exception):
    """Raised rather than let an extraction land on or beside source media.

    AGENTS.md makes the relationship to source footage read-only, and the one
    exception here -- extracting a range so CPU transcription stays viable --
    was authorised on the condition that it writes to an analysis directory.
    A path bug should not be the only thing between a mistake and someone's
    camera original.
    """


def audio_extract_argv(source_path, output_path, *, start_seconds, end_seconds):
    """ffmpeg command to pull one range of a clip's audio as 16k mono WAV.

    Extracting a whole file would be pointless -- whisperx decodes internally
    anyway -- but extracting a range is not. Eight seconds of a two-hour clip
    is the difference between usable and unusable when the ASR runs on CPU.
    """
    source_dir = os.path.dirname(os.path.abspath(source_path))
    output_dir = os.path.dirname(os.path.abspath(output_path))
    if os.path.abspath(source_path) == os.path.abspath(output_path):
        raise SourceWriteRefused(
            f"refusing to write the extract over its own source: {source_path}")
    if source_dir == output_dir:
        raise SourceWriteRefused(
            f"refusing to write an extract into the source directory "
            f"{source_dir}; analysis output belongs elsewhere")
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        # Seek before -i so ffmpeg does not decode everything up to the range.
        "-ss", str(float(start_seconds)),
        "-i", source_path,
        # Duration, not an absolute end: with -ss applied first, -to is
        # measured from the seek point on some builds and from zero on others.
        # -t means the same thing everywhere.
        "-t", str(float(end_seconds) - float(start_seconds)),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        output_path,
    ]

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


def speaker_filename_stem(speaker):
    """A filename fragment that cannot escape its directory.

    `os.path.basename` alone would not do: a label of ".." survives it.
    """
    cleaned = _UNSAFE_IN_FILENAME.sub("_", speaker or "").strip("._-")
    return cleaned or UNDIARIZED_STEM


def write_speaker_srt_files(payload, out_dir, prefix=""):
    """Write one SRT per speaker and return what was written.

    Resolve imports subtitle files, not transcript objects, and it puts one
    file on one track -- so a track per voice means a file per voice.

    Returns a list of {speaker, path, cue_count} in first-appearance order.
    """
    from src.utils.media_analysis import segments_to_srt

    os.makedirs(out_dir, exist_ok=True)
    written = []
    for speaker, segments in group_segments_by_speaker(
            payload.get("segments") or []).items():
        stem = speaker_filename_stem(speaker)
        path = os.path.join(out_dir, f"{prefix}{stem}.srt")
        # segments_to_srt numbers cues from one over whatever list it gets, so
        # each file is independently valid rather than carrying gaps where the
        # other speaker's cues were.
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(segments_to_srt(segments))
        written.append({
            "speaker": speaker,
            "path": path,
            "cue_count": len(segments),
        })
    return written
