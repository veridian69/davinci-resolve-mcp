"""WhisperX transcription and speaker-diarized subtitle support.

Holds the whole subtitle feature: `timing.py` (frame/second conversions between
source media and timeline), `srt.py` (SRT writing, speaker grouping and the
source-write refusal guard), `whisperx.py` (the whisperX backend - argv and env
construction, executable resolution, transcription, and the speaker labels
upstream's normalizer drops) and `tools.py` (the two `@mcp.tool()` entry points
that transcribe a timeline item and import the result as subtitle tracks).

It lives here because upstream knows nothing about whisperX and must keep
knowing nothing: `free_edition.integrate.register_whisperx()` installs the
backend into `src.utils.media_analysis` at runtime, and
`register_subtitle_tools()` registers the two tools against the granular FastMCP
instance, replacing what used to be static edits to upstream files. Code here
may still read *from* pristine upstream helpers, so the checkout root has to be
on `sys.path` at call time.
"""
