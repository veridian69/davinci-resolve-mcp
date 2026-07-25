"""Validate whisperx_transcribe_timeline_item + whisperx_import_subtitles
end to end against a live Resolve session.

Unlike the other tools/probe_*.py scripts, this one needs a real timeline
item with real audio -- there is no way to synthesize speech from inside
Resolve's embedded interpreter the way tools that ran outside it could shell
out to macOS `say`. So this picks the first audio-bearing item off the
CURRENT timeline (or one named by TIMELINE_ITEM_ID) rather than building its
own scratch clip.

Run AFTER the normal boot, so the shim, granular tools, and whisperx wiring
are already in place:

    INPROC_REPO="/path/to/davinci-resolve-mcp"; exec(open(INPROC_REPO+"/resolve_console_boot.py").read())
    exec(open("/path/to/davinci-resolve-mcp/tools/live_subtitles_validation.py").read())

Optional, before pasting, to target a specific item instead of the first one
found:

    TIMELINE_ITEM_ID = "the-unique-id"

WHAT THIS DOES TO YOUR PROJECT
  It transcribes real audio (network/CPU cost, whisperx must be installed --
  see README-FREE-EDITION.md) and, unlike tools/probe_subtitle_import.py,
  LEAVES the resulting subtitle tracks in place rather than deleting them --
  the point is to look at them. Uses --model tiny for speed; diarization is
  off by default since it needs an HF_TOKEN this script does not manage (set
  DIARIZE = True and export HF_TOKEN yourself to exercise that path).

ASCII-only, like every file the console reads.
"""

import os


def _line(label, value):
    print("  {:<28} {}".format(label, value))


DIARIZE = False


def _pick_audio_item(timeline):
    for track_type in ("audio", "video"):
        for track_index in range(1, timeline.GetTrackCount(track_type) + 1):
            for item in timeline.GetItemListInTrack(track_type, track_index) or []:
                mpi = item.GetMediaPoolItem()
                if mpi and mpi.GetClipProperty("File Path"):
                    return item
    return None


def main():
    resolve = globals().get("resolve")
    if resolve is None:
        print("no `resolve` in the console namespace -- use the Py3 dropdown")
        return

    bridge = __import__("sys").modules.get("DaVinciResolveScript")
    if getattr(bridge, "__file__", None) != "<inproc-shim>":
        print("shim not installed -- run resolve_console_boot.py first")
        return

    granular = __import__("sys").modules.get("src.granular")
    if granular is None or not hasattr(granular, "mcp"):
        print("src.granular not imported -- run resolve_console_boot.py first")
        return

    from src.granular.subtitles import (
        whisperx_import_subtitles,
        whisperx_transcribe_timeline_item,
    )

    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        print("no project open")
        return
    timeline = project.GetCurrentTimeline()
    if not timeline:
        print("no current timeline")
        return

    item_id = globals().get("TIMELINE_ITEM_ID")
    if item_id:
        from src.granular.subtitles import _find_timeline_item
        item = _find_timeline_item(timeline, item_id)
    else:
        item = _pick_audio_item(timeline)
    if item is None:
        print("no timeline item with a media pool file path found -- "
              "set TIMELINE_ITEM_ID or add one to the current timeline")
        return
    item_id = str(item.GetUniqueId())

    print("=" * 68)
    print("LIVE SUBTITLES VALIDATION")
    print("=" * 68)
    _line("timeline", timeline.GetName())
    _line("item", "{} (id={})".format(item.GetName(), item_id))

    print()
    print("-- whisperx_transcribe_timeline_item")
    transcribe_args = {"timeline_item_id": item_id, "model": "tiny"}
    if DIARIZE:
        transcribe_args.update(diarize=True, min_speakers=1, max_speakers=4)
    result = whisperx_transcribe_timeline_item(**transcribe_args)
    for key, value in result.items():
        _line(key, value)
    if not result.get("success"):
        print()
        print("transcription failed -- stopping before import")
        return

    print()
    print("-- whisperx_import_subtitles")
    imported = whisperx_import_subtitles(transcript_json=result["transcript_json"])
    if not imported.get("success"):
        _line("error", imported.get("error"))
        return
    for track in imported["tracks_created"]:
        _line("track {}".format(track["track_index"]),
              "{!r} -- {} cues -- {}".format(
                  track["track_name"], track["cue_count"], track["srt_path"]))

    print()
    print("subtitle tracks are LIVE on the timeline -- inspect them in Resolve.")
    print("delete them by hand (or DeleteTrack('subtitle', N)) when done looking.")


main()
