"""Pure time conversions between a transcript and a Resolve timeline.

No I/O, no Resolve objects, no whisperX. Everything here takes numbers and
returns numbers, because this is where subtitle bugs live and a function that
touches nothing else can be tested exhaustively.

Three clocks are in play and confusing any two of them produces subtitles that
look plausible and are wrong:

  extract   what whisperX returns, zero at the start of the audio we handed it
  source    extract + the ffmpeg -ss offset; frame zero is the head of the media
  timeline  where the clip actually sits, including the start timecode
"""


class RetimeNotSupported(Exception):
    """Raised for a clip whose speed is not 100%.

    The source-to-timeline mapping here is linear, which is only true at full
    speed. On a 50% clip a word one minute in belongs two minutes in, so the
    error grows across the clip instead of staying constant -- the kind of
    drift that looks fine at the head of a cut and is obviously broken by the
    end. Refusing names the clip; mapping it anyway would not.
    """


# Speeds are floats coming out of Resolve, so compare with a tolerance rather
# than against the literal 100.0.
_SPEED_TOLERANCE = 1e-6


def source_seconds_to_timeline_frame(
    source_seconds,
    *,
    frame_rate,
    source_start_frame,
    record_start_frame,
    speed_percent=100.0,
):
    """Map a timestamp in source media time to an absolute timeline frame.

    Args:
        source_seconds: position in the source media, seconds.
        frame_rate: the timeline's rate, e.g. 24.0 or 24000/1001.
        source_start_frame: TimelineItem.GetSourceStartFrame() -- which frame of
            the media the clip starts on.
        record_start_frame: TimelineItem.GetStart() -- where the clip sits on
            the timeline, absolute, including the start timecode.
        speed_percent: 100.0 for an untouched clip. Anything else raises.

    Raises:
        RetimeNotSupported: when speed_percent is not 100.
    """
    if abs(speed_percent - 100.0) > _SPEED_TOLERANCE:
        raise RetimeNotSupported(
            f"clip is retimed to {speed_percent}%; subtitle timing supports "
            f"100% only"
        )
    # Rounded once, at the end. Rounding the seconds->frames product first and
    # then applying the offsets accumulates error across a long clip.
    return round(
        source_seconds * frame_rate - source_start_frame + record_start_frame
    )


def timeline_frame_to_srt_seconds(
    timeline_frame,
    *,
    frame_rate,
    timeline_start_frame,
):
    """Convert an absolute timeline frame to a cue time for an SRT file.

    Args:
        timeline_frame: absolute frame, as returned by the mapping above.
        frame_rate: the timeline's rate.
        timeline_start_frame: Timeline.GetStartFrame(). Resolve timelines
            conventionally start at 01:00:00:00, so this is rarely zero and
            forgetting it puts every cue in the file an hour late.
    """
    return (timeline_frame - timeline_start_frame) / frame_rate
