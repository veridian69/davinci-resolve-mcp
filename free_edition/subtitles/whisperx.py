"""The whisperX transcription backend, extracted out of upstream.

This module used to live inside `src/utils/media_analysis.py`. It moved here so
that file can go back to being byte-identical to upstream; nothing in `src/`
imports this module, and `free_edition.integrate.register_whisperx()` is what
wires it into `src.utils.media_analysis` at runtime (registry entry, capability
probe, and the `_transcribe` dispatcher).

What is here:

* `TOOL_INSTALL_ENTRY` -- the value `integrate` assigns to
  `media_analysis.TOOL_INSTALL["whisperx"]`.
* The four flag tables and `WHISPERX_DEFAULT_OUTPUT_FORMAT`, which describe
  whisperX's CLI surface.
* `_whisperx_env` / `_whisperx_argv` / `_resolve_whisperx_executable` /
  `_detect_ffmpeg_lib_dir`, the command construction.
* `_run_command`, a local copy of upstream's runner that also takes `env=`.
* `_transcribe_with_whisperx`, the entry point, and
  `_reattach_speaker_labels`, which puts back the diarization labels upstream's
  parser drops. See that function for why it has to exist.

Upstream helpers are reached through `_upstream()` at call time rather than by
a module-scope `from src.utils.media_analysis import ...`. Importing this module
therefore costs nothing and pulls in no upstream code, which keeps it usable
before the boot has imported `src.*` and keeps it honest about the
`sys.modules` purge the boot scripts perform.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# The `media_analysis.TOOL_INSTALL["whisperx"]` value. Held here as a plain
# constant because installing it is a one-line dict-item assignment that
# `integrate.register_whisperx()` performs: `install_plan_for()` reads
# `TOOL_INSTALL.get(tool_name)` at call time, so the key can arrive after the
# literal has been built and no wrapper is needed.
TOOL_INSTALL_ENTRY: Dict[str, Any] = {
    "label": "whisperX",
    "bundle": "transcription",
    "required_for": [
        "word-level subtitle timing (forced alignment)",
        "speaker diarization",
    ],
    "commands": {
        # Its own venv, not the server's: whisperx pins Python <3.14 and
        # pulls torch and pyannote, which must not land in this interpreter.
        "all": (
            "python3.12 -m venv ~/.whisperx-venv && "
            "~/.whisperx-venv/bin/pip install whisperx, then set "
            "WHISPERX_BIN=~/.whisperx-venv/bin/whisperx"
        ),
    },
    "verify": "whisperx --version",
    "notes": (
        "Requires Python >=3.10,<3.14 and ~2-3 GB of dependencies. The ASR "
        "stage runs on CPU on Apple Silicon -- CTranslate2 has no Metal "
        "backend -- so expect it to be slow there. Diarization additionally "
        "needs a Hugging Face token with the pyannote gated models accepted; "
        "pass it as HF_TOKEN in the environment, never on the command line."
    ),
}


# json carries the word timings and speaker labels; srt and vtt flatten them
# away, and the backend parses the JSON. "all" is a legitimate override -- it
# writes every format in one pass, so a caller who wants subtitle files gets
# them without a second transcription, and the JSON is still there to parse.
WHISPERX_DEFAULT_OUTPUT_FORMAT = "json"


# Options forwarded verbatim as `--flag value`. The point of this backend is to
# be a thin wrapper, so the caller reaches whisperx's own surface rather than a
# reinvented one. Deliberately absent: hf_token, which travels in the
# environment instead -- see _whisperx_env.
WHISPERX_VALUE_FLAGS = (
    "model", "model_dir", "language", "task", "batch_size", "threads",
    "align_model", "interpolate_method", "vad_method", "vad_onset",
    "vad_offset", "chunk_size", "min_speakers", "max_speakers",
    "diarize_model", "temperature", "beam_size", "best_of", "patience",
    "length_penalty", "initial_prompt", "hotwords", "suppress_tokens",
    "max_line_width", "max_line_count", "segment_resolution", "device_index",
    "temperature_increment_on_fallback", "compression_ratio_threshold",
    "logprob_threshold", "no_speech_threshold", "device", "compute_type",
)

# Options forwarded as bare `--flag` when truthy. These are argparse
# `action="store_true"` and take no value.
WHISPERX_BOOLEAN_FLAGS = (
    "diarize", "no_align", "return_char_alignments", "speaker_embeddings",
    "suppress_numerals",
)

# A third category, and the reason there are three: whisperx declares these as
# `type=str2bool`, so they take an explicit True/False. Emitted bare they would
# swallow the following argument as their value. False must survive too -- it
# turns a default-on option off, so it is a value rather than an absence.
WHISPERX_STR2BOOL_FLAGS = (
    "highlight_words", "print_progress", "condition_on_previous_text",
    "model_cache_only",
)


# Where a torchcodec-compatible FFmpeg might be sitting alongside the current
# one. Ordered newest first; a tuple so a test can replace it.
WHISPERX_FFMPEG_LIB_CANDIDATES = (
    "/opt/homebrew/opt/ffmpeg@7/lib",
    "/usr/local/opt/ffmpeg@7/lib",
    "/opt/homebrew/opt/ffmpeg@6/lib",
    "/usr/local/opt/ffmpeg@6/lib",
)


def _upstream():
    """`src.utils.media_analysis`, imported on demand.

    Deferred rather than imported at module scope so that importing
    `free_edition.subtitles.whisperx` never drags upstream in as a side effect.
    The boot scripts purge `src.*` and `free_edition.*` together and then
    re-import in a fixed order (shim, then upstream, then registration); a
    module-scope import here would resolve upstream at whatever moment this
    module first loaded and quietly outlive the purge.
    """
    import src.utils.media_analysis as media_analysis

    return media_analysis


def _run_command(args: List[str], timeout: Optional[int] = None,
                 env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    """Upstream's `_run_command`, plus `env=`.

    Copied rather than reused because passing an environment is the one thing
    upstream's runner cannot do, and widening its signature would mean editing
    `src/utils/media_analysis.py` -- the file this whole layer exists to leave
    alone. The four upstream call sites never pass `env`, so nothing over there
    needs the parameter.

    `timeout=None` defers to upstream's `COMMAND_TIMEOUT_SECONDS` at call time
    instead of hardcoding 300 here, so the default cannot drift away from
    upstream's. The only caller in this module always passes one explicitly.
    """
    if timeout is None:
        timeout = _upstream().COMMAND_TIMEOUT_SECONDS
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
        stderr_tail = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        return 124, stdout, f"Command timed out after {timeout}s. {stderr_tail}".strip()
    except OSError as exc:
        return 127, "", str(exc)
    stdout = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
    return proc.returncode, stdout, stderr


def _detect_ffmpeg_lib_dir() -> Optional[str]:
    """First candidate directory that actually holds a matching libavutil.

    Existence of the directory is not enough: Homebrew leaves ffmpeg@6 and
    ffmpeg@7 as symlinks to whatever ffmpeg is current after an upgrade, so
    `/opt/homebrew/opt/ffmpeg@7/lib` can exist and contain FFmpeg 8's
    libavutil.60 -- exactly the version torchcodec cannot use.
    """
    for candidate in WHISPERX_FFMPEG_LIB_CANDIDATES:
        for soname in ("libavutil.59.dylib", "libavutil.58.dylib"):
            if os.path.exists(os.path.join(candidate, soname)):
                return candidate
    return None


def _whisperx_env(transcription: Dict[str, Any], base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment for the whisperx subprocess, carrying the Hugging Face token.

    The token authorises pyannote's gated diarization models. It travels here
    rather than as `--hf_token` because anything on a command line is readable
    by every process on the machine through `ps`, and because argparse can echo
    arguments back in an error -- and this backend returns stderr verbatim to
    the caller on failure.
    """
    env = dict(os.environ if base_env is None else base_env)
    token = transcription.get("hf_token")
    if token:
        env["HF_TOKEN"] = str(token)

    # torchcodec ships dylibs linked against FFmpeg 4 through 7 and refuses to
    # load against FFmpeg 8, which is what Homebrew installs today. pyannote
    # then falls back to another decoder and prints a forty-line traceback as a
    # warning. Pointing the loader at a side-by-side FFmpeg 7 silences it.
    # Configured rather than guessed, because the location is per-machine.
    ffmpeg_lib = (transcription.get("ffmpeg_lib_dir")
                  or env.get("WHISPERX_FFMPEG_LIB")
                  or _detect_ffmpeg_lib_dir())
    if ffmpeg_lib:
        existing = env.get("DYLD_LIBRARY_PATH")
        env["DYLD_LIBRARY_PATH"] = (f"{ffmpeg_lib}:{existing}" if existing
                                    else str(ffmpeg_lib))
    return env


def _whisperx_argv(executable: str, audio_path: str, output_dir: str, transcription: Dict[str, Any]) -> List[str]:
    """Build the whisperx command line."""
    # device and compute_type are deliberately absent: whisperx already
    # resolves them (`"cuda" if torch.cuda.is_available() else "cpu"`, and a
    # compute type of "default" that means float16 on GPU, float32 on CPU).
    # Sending our own would override that detection -- pinning "cpu" would turn
    # off a GPU that exists, and pinning int8 would trade accuracy the caller
    # never agreed to give up. They pass through below when asked for.
    coerce_bool = _upstream()._coerce_bool
    argv = [
        executable,
        audio_path,
        "--output_dir", output_dir,
        "--output_format", str(transcription.get("output_format")
                               or WHISPERX_DEFAULT_OUTPUT_FORMAT),
    ]
    for name in WHISPERX_BOOLEAN_FLAGS:
        if coerce_bool(transcription.get(name), default=False):
            argv.append(f"--{name}")
    for name in WHISPERX_STR2BOOL_FLAGS:
        if name in transcription and transcription[name] is not None:
            argv.extend([f"--{name}",
                         "True" if coerce_bool(transcription[name],
                                               default=False) else "False"])
    for name in WHISPERX_VALUE_FLAGS:
        value = transcription.get(name)
        if value is not None and value != "":
            argv.extend([f"--{name}", str(value)])
    return argv


def _resolve_whisperx_executable(transcription: Dict[str, Any]) -> Optional[str]:
    """Locate the whisperx CLI.

    It lives in its own virtualenv rather than on the default PATH: whisperx
    requires Python >=3.10,<3.14 and pulls in torch and pyannote, so it cannot
    share an interpreter with this server. WHISPERX_BIN is how a caller points
    at that venv without putting it on PATH globally.
    """
    explicit = transcription.get("executable") or os.environ.get("WHISPERX_BIN")
    if explicit:
        return str(explicit) if os.path.exists(str(explicit)) else None
    return shutil.which("whisperx")


# Sentinel for "this key was not present", so `_reattach_speaker_labels` can
# tell a missing "words" key from a present-but-empty one while it reorders.
_MISSING = object()


def _reattach_speaker_labels(raw: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """Put the diarization speaker labels back after upstream normalisation.

    WHY THIS EXISTS
    ---------------
    While this backend lived inside `src/utils/media_analysis.py` it relied on
    two passthroughs we had added *inside* upstream's parsers -- six lines in
    `_normalize_transcript_payload` and six in `_normalize_word_timestamps` --
    that copied `speaker` from the raw whisperX JSON onto the normalised
    output. Those edits are exactly what stopped that file being byte-identical
    to upstream, so they are gone. Pristine upstream builds each normalised
    segment from `start` / `end` / `text` only, and each normalised word from
    `word` / `start` / `end` / `probability` / `confidence` / `score` only:
    every `speaker` key is dropped on the floor.

    Without this post-pass the failure is silent and total. Transcription still
    succeeds, `transcript.json` is still written, `whisperx_import_subtitles`
    still runs -- and produces exactly ONE subtitle track called "Unassigned",
    because `group_segments_by_speaker` and `split_segments_on_speaker_change`
    have nothing left to split on. Nothing raises and no log line points at it.

    A post-pass rather than a monkeypatch of the two upstream parsers: patching
    them would mean carrying a copy of upstream's parsing logic that silently
    goes stale the next time upstream touches it, and it would also change the
    output of every other backend that shares those parsers.

    HOW THE ALIGNMENT IS SAFE
    -------------------------
    Positional zip, which is exact because upstream's loops do not filter:

    * Segments: `for segment in raw.get("segments") or []` has no isinstance
      guard and no `continue`, and appends once per iteration, so there is
      exactly one normalised segment per raw segment.
    * Words: `_normalize_word_timestamps` returns `[]` for a non-list, skips
      raw entries that are not dicts (`continue`), and otherwise appends once
      per entry -- so the raw side of the zip is
      `[w for w in (raw_words or []) if isinstance(w, dict)]`.
    * `payload["words"]` is either the same list objects already fixed by the
      per-segment pass (upstream does `all_words.extend(words)`, which stores
      references, so mutating a segment's word dict updates the flat list too)
      or, when the raw payload carries a top-level "words" key that normalises
      to something non-empty, a separately parsed list that needs its own pass.
      The condition below reproduces upstream's `if top_level_words:` exactly.

    Mutates `payload` in place and returns it.
    """
    for raw_segment, normalized_segment in zip(raw.get("segments") or [],
                                               payload.get("segments") or []):
        speaker = raw_segment.get("speaker")
        if speaker and not normalized_segment.get("speaker"):
            # Re-inserted before "words" so the key order matches what the old
            # in-upstream passthrough produced: start, end, text, speaker,
            # words. Nothing depends on it, but it keeps transcript.json
            # byte-comparable against transcripts written before this refactor.
            words = normalized_segment.pop("words", _MISSING)
            normalized_segment["speaker"] = str(speaker)
            if words is not _MISSING:
                normalized_segment["words"] = words

        _reattach_word_speakers(raw_segment.get("words"),
                                normalized_segment.get("words") or [])

    # The top-level "words" branch. Upstream replaces the accumulated
    # per-segment words with this list whenever it normalises to something
    # non-empty, which is precisely when raw["words"] is a list holding at
    # least one dict -- so that same test decides whether payload["words"] is
    # an independent list still needing labels, or aliases of dicts the loop
    # above already fixed.
    raw_top_level = raw.get("words")
    if isinstance(raw_top_level, list) and any(
            isinstance(word, dict) for word in raw_top_level):
        _reattach_word_speakers(raw_top_level, payload.get("words") or [])
    return payload


def _reattach_word_speakers(raw_words: Any, normalized_words: List[Dict[str, Any]]) -> None:
    """Copy `speaker` onto normalised words, matching upstream's skip rule.

    Upstream drops non-dict entries with `continue`, so the raw side has to be
    filtered the same way or every word after a malformed entry gets the
    previous word's label.
    """
    if not isinstance(raw_words, list):
        return
    candidates = [word for word in raw_words if isinstance(word, dict)]
    for raw_word, normalized_word in zip(candidates, normalized_words):
        speaker = raw_word.get("speaker")
        if speaker and not normalized_word.get("speaker"):
            normalized_word["speaker"] = str(speaker)


def _transcribe_with_whisperx(path: str, artifacts: Dict[str, Any], transcription: Dict[str, Any]) -> Dict[str, Any]:
    executable = _resolve_whisperx_executable(transcription)
    if not executable:
        return {
            "success": False,
            "status": "skipped",
            "backend": "whisperx",
            "reason": (
                "whisperx not found. It needs its own virtualenv on Python "
                "3.10-3.13; point WHISPERX_BIN at that venv's whisperx."
            ),
        }
    media_analysis = _upstream()
    work_dir = os.path.join(
        os.path.dirname(artifacts.get("transcript_json") or artifacts["analysis_json"]),
        "whisperx-work",
    )
    os.makedirs(work_dir, exist_ok=True)
    argv = _whisperx_argv(executable, path, work_dir, transcription)
    env = _whisperx_env(transcription)
    code, _, stderr = _run_command(argv, timeout=int(transcription.get("timeout", 1800)), env=env)
    if code != 0:
        return {"success": False, "backend": "whisperx", "error": stderr.strip() or "whisperx failed"}
    json_files = sorted(Path(work_dir).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not json_files:
        return {"success": False, "backend": "whisperx", "error": "whisperx produced no JSON output"}
    raw = media_analysis._read_json(str(json_files[0]))
    payload = media_analysis._normalize_transcript_payload(raw, "whisperx", transcription.get("language"))
    # Before the artifacts are written, not after: transcript.json and the SRT
    # are derived from `payload`, and a transcript on disk without speakers is
    # indistinguishable from a clip with one speaker.
    _reattach_speaker_labels(raw if isinstance(raw, dict) else {}, payload)
    media_analysis._write_transcript_artifacts(payload, artifacts)
    return payload
