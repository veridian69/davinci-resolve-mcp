"""Subtitle generation: whisperX transcription -> per-speaker tracks in Resolve.

Two tools, deliberately not one: transcription takes minutes and fails for
its own reasons (missing model, bad token, low memory); import is a handful
of fast Resolve API calls. Merging them would mean an import failure forces
a re-transcribe of everything.

  whisperx_transcribe_timeline_item  wraps the backend in
                                     free_edition/subtitles/whisperx.py.
                                     Does not touch Resolve beyond reading the
                                     clip's file path and frame position.
  whisperx_import_subtitles          reads a transcript JSON, splits it by
                                     speaker, writes one SRT per voice, and
                                     puts each on its own subtitle track.

Both @mcp.tool() decorators fire on the FastMCP instance that
`from src.granular.common import *` puts in this module's namespace -- which is
why this file is imported for its side effect by
`free_edition.integrate.register_subtitle_tools()` at boot, rather than being
listed in src/granular/__init__.py. Upstream stays byte-identical; the two
tools still land on the granular server. That import must happen before
`dispatch.install()`, or these two tools run unserialised against the Resolve
bridge (see the ordering contract in free_edition/integrate.py).

Track routing (build_subtitle_track_plan) is pure and unit-tested in
free_edition/tests/test_subtitles_import.py. The Resolve calls around it --
AddTrack, SetTrackName, ImportMedia, AppendToTimeline -- follow the measured
shape from free_edition/tools/probe_subtitle_import.py: ImportMedia accepts
.srt directly, and AppendToTimeline's clipInfo form (already used by
append_to_timeline in media_pool.py) takes trackIndex explicitly. Validated end
to end against a live Resolve session by
free_edition/tools/live_subtitles_validation.py.
"""

import os
import pathlib

from src.granular.common import *  # noqa: F401,F403
from free_edition.subtitles.srt import (
    audio_extract_argv,
    write_speaker_srt_files,
)
from free_edition.subtitles.whisperx import _transcribe_with_whisperx

resolve = ResolveProxy()

UNLABELLED_TRACK_NAME = "Unassigned"

# The .inproc scratch tree lives at the checkout root, two levels above this
# file. Derived once and then checked: the form this replaced walked
# `dirname(__file__)/../..`, which is correct here only by coincidence --
# free_edition/subtitles/ happens to sit at the same depth src/granular/ did.
# Nest this module one level deeper without noticing and nothing would raise:
# os.makedirs would succeed, transcripts would be written, and they would
# simply land where nobody looks, or (one level shallower) outside the checkout
# altogether. An assertion at import time is the loud version of that bug.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
assert (_REPO_ROOT / "src" / "granular").is_dir(), (
    f"expected the checkout root two levels above this file, got {_REPO_ROOT}, "
    f"where {_REPO_ROOT / 'src' / 'granular'} is not a directory -- "
    f"free_edition/subtitles/tools.py has moved relative to the repo root"
)


def build_subtitle_track_plan(written_files):
    """Decide which 1-based subtitle track index each speaker's SRT lands on.

    Order follows write_speaker_srt_files, which is first-appearance order --
    track 1 belongs to whoever speaks first, not to whichever label sorts
    lowest. A single entry does not need explicit routing: Resolve's
    AppendToTimeline simple-list form lands it on the one subtitle track that
    exists, which is what free_edition/tools/probe_subtitle_import.py measured.
    Explicit trackIndex only earns its complexity once there is more than one
    voice to keep apart.
    """
    plan = []
    for index, entry in enumerate(written_files, start=1):
        speaker = entry.get("speaker") or ""
        plan.append({
            "speaker": speaker,
            "path": entry["path"],
            "cue_count": entry.get("cue_count"),
            "track_index": index,
            "track_name": speaker or UNLABELLED_TRACK_NAME,
            "needs_explicit_track_index": len(written_files) > 1,
        })
    return plan


def _find_timeline_item(timeline, timeline_item_id):
    """Search every video/audio/subtitle track for a unique_id, the pattern
    repeated per-tool throughout src/granular/timeline_item.py. Kept local
    here rather than factored out repo-wide -- refactoring code this change
    did not touch is out of scope."""
    for track_type in ("video", "audio", "subtitle"):
        for track_index in range(1, timeline.GetTrackCount(track_type) + 1):
            for item in timeline.GetItemListInTrack(track_type, track_index) or []:
                if str(item.GetUniqueId()) == str(timeline_item_id):
                    return item
    return None


@mcp.tool()
def whisperx_transcribe_timeline_item(
    timeline_item_id: str,
    model: str = None,
    language: str = None,
    diarize: bool = False,
    min_speakers: int = None,
    max_speakers: int = None,
    hf_token: str = None,
    extract_range: bool = True,
    output_dir: str = None,
    executable: str = None,
    device: str = None,
    compute_type: str = None,
) -> Dict[str, Any]:
    """Transcribe a timeline item's source media with whisperX.

    A thin wrapper: this tool's job ends at a transcript JSON on disk. It does
    not touch Resolve beyond reading the item's source file path and frame
    position, and it does not write subtitles -- that is
    whisperx_import_subtitles, run separately so a transcription failure
    (model, token, memory) never forces re-importing and an import failure
    never forces re-transcribing.

    Args:
        timeline_item_id: unique_id of the timeline item to transcribe, as
            returned by timeline tools (e.g. append_to_timeline).
        model, language, diarize, min_speakers, max_speakers, hf_token,
            executable, device, compute_type: forwarded to whisperx. hf_token
            travels as HF_TOKEN in the subprocess environment, never on the
            command line -- see _whisperx_env in
            free_edition/subtitles/whisperx.py.
        extract_range: when true (default) and the item has a normal
            (non-retimed) speed, only the range this timeline item actually
            uses is transcribed via ffmpeg, not the whole source file. Falls
            back to the whole file if the item is retimed or the range cannot
            be determined, rather than refusing outright -- retimed audio
            still transcribes correctly, only its subtitle *timing* mapping
            (a separate, later step) cannot be trusted.
        output_dir: where the transcript JSON/artifacts land. Defaults to
            .inproc/subtitles/<timeline_item_id>/ next to this checkout.
    """
    r = get_resolve()
    if r is None:
        return {"error": "Not connected to DaVinci Resolve"}
    project, tl, err = _get_timeline()
    if err:
        return err

    item = _find_timeline_item(tl, timeline_item_id)
    if item is None:
        return {"error": f"Timeline item with ID '{timeline_item_id}' not found"}

    mpi = item.GetMediaPoolItem()
    if not mpi:
        return {"error": "Timeline item has no media pool item (it may be a generator or title)"}
    source_path = mpi.GetClipProperty("File Path")
    if not source_path or not os.path.exists(source_path):
        return {"error": f"Source media not found on disk: {source_path!r}"}

    out_dir = output_dir or os.path.join(
        str(_REPO_ROOT), ".inproc", "subtitles", str(timeline_item_id))
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    transcribe_path = source_path
    range_note = "whole source file"
    if extract_range:
        try:
            frame_rate = float(tl.GetSetting("timelineFrameRate") or 0) or 24.0
            source_start_frame = item.GetSourceStartFrame()
            source_end_frame = item.GetSourceEndFrame()
            start_seconds = source_start_frame / frame_rate
            end_seconds = source_end_frame / frame_rate
        except Exception as exc:
            return {"error": f"Could not read the item's source range: {exc}"}
        extract_path = os.path.join(out_dir, "extract.wav")
        import subprocess
        argv = audio_extract_argv(source_path, extract_path,
                                  start_seconds=start_seconds, end_seconds=end_seconds)
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0:
            return {"error": f"ffmpeg range extraction failed: {proc.stderr.strip()[:2000]}"}
        transcribe_path = extract_path
        range_note = f"{start_seconds:.3f}s-{end_seconds:.3f}s of source"

    transcription = {"diarize": diarize}
    for key, value in (
        ("model", model), ("language", language),
        ("min_speakers", min_speakers), ("max_speakers", max_speakers),
        ("hf_token", hf_token), ("executable", executable),
        ("device", device), ("compute_type", compute_type),
    ):
        if value is not None:
            transcription[key] = value

    artifacts = {
        "analysis_json": os.path.join(out_dir, "analysis.json"),
        "transcript_json": os.path.join(out_dir, "transcript.json"),
        "transcript_srt": os.path.join(out_dir, "transcript.srt"),
    }
    result = _transcribe_with_whisperx(transcribe_path, artifacts, transcription)
    if not result.get("success"):
        return {
            "success": False,
            "error": result.get("error") or result.get("reason") or "whisperx transcription failed",
            "status": result.get("status"),
        }
    return {
        "success": True,
        "timeline_item_id": timeline_item_id,
        "transcript_json": artifacts["transcript_json"],
        "segment_count": len(result.get("segments") or []),
        "language": result.get("language"),
        "transcribed": range_note,
    }


@mcp.tool()
def whisperx_import_subtitles(
    transcript_json: str,
    output_dir: str = None,
) -> Dict[str, Any]:
    """Import a whisperx transcript as one subtitle track per speaker.

    Splits the transcript by speaker (correcting for whisperx's per-segment
    label, which is a majority vote and lies on any chunk mixing more than one
    voice -- see split_segments_on_speaker_change), writes one SRT per voice,
    then for each: adds a subtitle track, names it after the speaker, imports
    the SRT into the media pool, and appends it to that track.

    Measured against a live Resolve session
    (free_edition/tools/probe_subtitle_import.py): ImportMedia accepts .srt
    directly and Resolve parses its cues correctly. A single speaker uses
    AppendToTimeline's simple list form; more than one uses the clipInfo form
    with explicit trackIndex so voices land on separate tracks instead of
    piling onto the same one.

    Args:
        transcript_json: path written by whisperx_transcribe_timeline_item.
        output_dir: where the per-speaker .srt files are written. Defaults to
            the transcript's own directory.
    """
    r = get_resolve()
    if r is None:
        return {"error": "Not connected to DaVinci Resolve"}
    project, tl, err = _get_timeline()
    if err:
        return err

    if not os.path.exists(transcript_json):
        return {"error": f"Transcript not found: {transcript_json}"}
    import json
    try:
        with open(transcript_json, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        return {"error": f"Could not read transcript JSON: {exc}"}

    out_dir = output_dir or os.path.dirname(os.path.abspath(transcript_json))
    written = write_speaker_srt_files(payload, out_dir)
    if not written:
        return {"error": "No subtitle segments in transcript"}

    plan = build_subtitle_track_plan(written)

    mp = project.GetMediaPool()
    root = mp.GetRootFolder()
    tracks_out = []
    for entry in plan:
        added = tl.AddTrack("subtitle")
        if not added:
            return {"success": False, "error": "AddTrack('subtitle') failed",
                   "tracks_created": tracks_out}
        track_index = tl.GetTrackCount("subtitle")
        tl.SetTrackName("subtitle", track_index, entry["track_name"])

        imported = mp.ImportMedia([entry["path"]])
        if not imported:
            return {"success": False, "error": f"ImportMedia failed for {entry['path']}",
                   "tracks_created": tracks_out}
        clip = imported[0]

        if entry["needs_explicit_track_index"]:
            duration_frames = clip.GetClipProperty("Frames")
            try:
                end_frame = int(duration_frames)
            except (TypeError, ValueError):
                end_frame = 999999999
            appended = mp.AppendToTimeline([{
                "mediaPoolItem": clip,
                "startFrame": 0,
                "endFrame": max(end_frame - 1, 0),
                "recordFrame": 0,
                "trackIndex": track_index,
            }])
        else:
            appended = mp.AppendToTimeline([clip])
        if not appended:
            return {"success": False, "error": f"AppendToTimeline failed for {entry['path']}",
                   "tracks_created": tracks_out}

        tracks_out.append({
            "speaker": entry["speaker"] or None,
            "track_index": track_index,
            "track_name": entry["track_name"],
            "srt_path": entry["path"],
            "cue_count": entry["cue_count"],
        })

    return {"success": True, "tracks_created": tracks_out}


__all__ = [name for name in globals() if not name.startswith("__")]
