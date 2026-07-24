"""Find out whether subtitles can be put on a timeline from the API, free edition.

This is the one unknown the whisperX subtitle feature depends on. The Resolve
scripting reference documents subtitle *tracks* (AddTrack, GetTrackCount,
GetItemListInTrack all take "subtitle") but documents no way to create a
subtitle *item*. CreateSubtitlesFromAudio is the only documented producer, and
it is Studio-only. So: can an .srt get in some other way?

Run from Resolve: Workspace -> Console -> Py3 dropdown, then:

    exec(open("/path/to/davinci-resolve-mcp/tools/probe_subtitle_import.py").read())

WHAT THIS DOES TO YOUR PROJECT
  It writes. It creates its own scratch timeline, adds a subtitle track to it,
  and imports a three-line .srt into the media pool. Everything it adds is
  recorded and removed in the cleanup phase, which runs even when a step
  fails, and the timeline you had open is restored as current.

  It only touches your current timeline if CreateEmptyTimeline is refused, and
  even then it removes only the tracks it added itself.

ASCII-only on purpose: the console's open() defaults to US-ASCII, so a single
accented character here fails the paste with UnicodeDecodeError.
"""

import os
import traceback

REPO = globals().get("INPROC_REPO") or os.environ.get("INPROC_REPO")
SRT_NAME = "probe_subtitle.srt"
SCRATCH_TIMELINE_NAME = "probe_subtitle_scratch"

# Deliberately ASCII. The point of the probe is the import path, not encoding,
# and a non-ASCII payload would confound a failure here with the encoding bug.
SRT_BODY = (
    "1\n"
    "00:00:00,500 --> 00:00:01,000\n"
    "probe one\n"
    "\n"
    "2\n"
    "00:00:01,000 --> 00:00:01,500\n"
    "probe two\n"
    "\n"
    "3\n"
    "00:00:01,500 --> 00:00:02,000\n"
    "probe three\n"
)


def _line(label, value):
    print("  {:<34} {}".format(label, value))


def _describe(obj):
    """Name the remote type without trusting hasattr, which always says True."""
    if obj is None:
        return "None"
    if isinstance(obj, (list, tuple)):
        return "{} of {} -> [{}]".format(
            type(obj).__name__, len(obj),
            ", ".join(_describe(o) for o in obj[:4]))
    if isinstance(obj, (bool, int, float, str)):
        return repr(obj)
    return "{} {}".format(type(obj).__name__, str(obj)[:70])


def main():
    resolve = globals().get("resolve")
    if resolve is None:
        print("no `resolve` in the console namespace -- use the Py3 dropdown")
        return

    scratch = REPO or os.path.expanduser("~")
    srt_path = os.path.join(scratch, SRT_NAME)

    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject() if pm else None
    if project is None:
        print("no project open")
        return
    media_pool = project.GetMediaPool()

    # Prefer a scratch timeline over the one the user is working in. The probe
    # adds a track and imports a clip; doing that to a real cut is rude even
    # with cleanup, and a cleanup that half-fails leaves the mess in the wrong
    # place. Falls back to the current timeline only if creation is refused.
    tl = None
    scratch_timeline = None
    previous_timeline = project.GetCurrentTimeline()
    try:
        scratch_timeline = media_pool.CreateEmptyTimeline(SCRATCH_TIMELINE_NAME)
    except Exception as exc:
        print("  CreateEmptyTimeline raised: {}: {}".format(
            type(exc).__name__, exc))
    if scratch_timeline is not None:
        project.SetCurrentTimeline(scratch_timeline)
        tl = scratch_timeline
    else:
        tl = previous_timeline

    if tl is None:
        print("no timeline available: could not create a scratch one and none "
              "is open. Open any timeline and re-run.")
        return

    print("=" * 68)
    print("SUBTITLE IMPORT PROBE")
    print("=" * 68)
    _line("product", "{} {}".format(resolve.GetProductName(),
                                    resolve.GetVersionString()))
    _line("project", project.GetName())
    _line("timeline", tl.GetName())

    before_tracks = tl.GetTrackCount("subtitle")
    _line("subtitle tracks before", before_tracks)

    # Recorded as we go so cleanup can undo exactly what happened, including
    # after a step raises.
    added_track = False
    imported = []

    try:
        print()
        print("-- 1. write the test .srt")
        # The console's open() is ASCII unless the MCP boot already patched it;
        # this payload is ASCII either way.
        with open(srt_path, "w") as fh:
            fh.write(SRT_BODY)
        _line("wrote", "{} ({} bytes)".format(srt_path, len(SRT_BODY)))

        print()
        print("-- 2. AddTrack('subtitle')")
        result = tl.AddTrack("subtitle")
        added_track = bool(result)
        _line("returned", _describe(result))
        _line("subtitle tracks now", tl.GetTrackCount("subtitle"))

        print()
        print("-- 3. MediaPool.ImportMedia([srt])")
        # The decisive call. Resolve's GUI imports .srt through the media pool,
        # but the API may simply refuse a non-media file.
        items = media_pool.ImportMedia([srt_path])
        _line("returned", _describe(items))
        if items:
            imported = [i for i in items if i is not None]
        for item in imported:
            _line("  clip name", _describe(item.GetName()))
            for prop in ("File Path", "Type", "Format", "Duration"):
                _line("  property " + prop, _describe(item.GetClipProperty(prop)))

        if not imported:
            print()
            print("  ImportMedia refused the .srt -- the SRT route is closed.")
            print("  Fallbacks: Text+ per cue on a video track, or an")
            print("  FCPXML/DRT round trip that creates a new timeline.")
        else:
            print()
            print("-- 4. AppendToTimeline(subtitle clip)")
            appended = media_pool.AppendToTimeline(imported)
            _line("returned", _describe(appended))
            _line("subtitle tracks now", tl.GetTrackCount("subtitle"))
            for idx in range(1, tl.GetTrackCount("subtitle") + 1):
                items_on_track = tl.GetItemListInTrack("subtitle", idx)
                _line("  items on subtitle track {}".format(idx),
                      _describe(items_on_track))

    except Exception:
        print()
        print("PROBE RAISED:")
        traceback.print_exc()

    finally:
        print()
        print("-- cleanup")
        try:
            if imported:
                ok = media_pool.DeleteClips(imported)
                _line("DeleteClips", _describe(ok))
        except Exception as exc:
            _line("DeleteClips failed", "{}: {}".format(type(exc).__name__, exc))
        if scratch_timeline is not None:
            # The whole timeline goes, tracks included, so there is nothing to
            # undo track by track.
            try:
                if previous_timeline is not None:
                    project.SetCurrentTimeline(previous_timeline)
                _line("DeleteTimelines(scratch)",
                      _describe(media_pool.DeleteTimelines([scratch_timeline])))
            except Exception as exc:
                _line("DeleteTimelines failed",
                      "{}: {}".format(type(exc).__name__, exc))
        else:
            try:
                now = tl.GetTrackCount("subtitle")
                # Only remove tracks this probe is responsible for, and only
                # the ones above the count we found on entry.
                if added_track and now > before_tracks:
                    for idx in range(now, before_tracks, -1):
                        _line("DeleteTrack subtitle {}".format(idx),
                              _describe(tl.DeleteTrack("subtitle", idx)))
                _line("subtitle tracks after cleanup",
                      tl.GetTrackCount("subtitle"))
            except Exception as exc:
                _line("DeleteTrack failed",
                      "{}: {}".format(type(exc).__name__, exc))
        try:
            if os.path.exists(srt_path):
                os.remove(srt_path)
                _line("removed", srt_path)
        except Exception as exc:
            _line("remove failed", "{}: {}".format(type(exc).__name__, exc))

    print()
    print("probe done")


main()
