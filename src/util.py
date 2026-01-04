import re
import subprocess
import asyncio
import json
import dataclasses
import aiohttp


@dataclasses.dataclass
class AttachmentOptions:
    max_video_dl_size: int
    max_audio_dl_size: int
    max_size: int | None
    audio_bitrate: int
    min_video_bitrate: int
    max_video_bitrate: int
    mp4_codec_a: str
    webm_codec_a: str


@dataclasses.dataclass
class FileStorageOptions:
    storage_path: str | None
    url_path: str | None
    tmp_file_path: str
    attachment: AttachmentOptions


@dataclasses.dataclass
class PlaybackOptions:
    NIGHTCORE_FACTOR_DOC = "Factor of how much to speed up the audio. [0.5, 1.5]."
    BASSBOOST_FACTOR_DOC = "Factor of how much to bassboost the audio. [-10, 10]."
    FILTER_GRAPH_DOC = "Custom ffmpeg audio filtering graph."
    START_TIMESTAMP_DOC = "Start timestamp of audio playback. e.g. 4:20"
    STOP_TIMESTAMP_DOC = "Stop timestamp of audio playback. e.g. 4:20"
    VOLUME_DOC = "Volume of the audio. [0, 200]."

    nightcore_factor: float | None = None
    bassboost_factor: float | None = None
    filter_graph: str | None = None
    start_timestamp: str | None = None
    stop_timestamp: str | None = None
    volume: float | None = None


def trim_option_text(text: str):
    if len(text) <= 100:
        return text
    return f"{text[:97]}..."


def duration_to_str(seconds: int):
    if seconds == 0:
        return "0 seconds"
    s = seconds % 60
    m = seconds // 60
    message = ""
    if m > 0:
        message += f"{m} minutes" if m > 1 else "1 minute"
    if s > 0:
        if message:
            message += " "
        message += f"{s} seconds" if s > 1 else "1 second"
    return message


def iso8601_to_unix_timestamp(text: str) -> int | None:
    match = re.match(r"PT((?P<h>\d+)H)?((?P<m>\d+)M)?((?P<s>\d+)S)?", text)
    if not match:
        return None
    result = 0
    h = match["h"]
    m = match["m"]
    s = match["s"]
    if h is not None:
        result += int(h) * 3600
    if m is not None:
        result += int(m) * 60
    if s is not None:
        result += int(s)
    return result


def yt_best_thumbnail_url(item: dict) -> str | None:
    return max(item["snippet"]["thumbnails"].values(), key=lambda e: e["height"])["url"]


def yt_video_data_from_url(url: str) -> dict | None:
    with subprocess.Popen(
        args=["yt-dlp", "--cookies", "cookie.txt", "-J", url],
        stdout=asyncio.subprocess.PIPE,
    ) as process:
        assert process.stdout
        data = process.stdout.read()
        if process.wait() != 0:
            return None
        return json.loads(data)


async def read_at_most(stream: aiohttp.streams.StreamReader, n: int) -> bytes:
    blocks: list[bytes] = []
    while n > 0 and not stream.at_eof():
        chunk = await stream.read(n)
        blocks.append(chunk)
        n -= len(chunk)
    return b"".join(blocks)


def audio_filter_graph(options: PlaybackOptions) -> str | None:
    filter_graph: list[str] = []
    if options.nightcore_factor:
        filter_graph.append(
            f"rubberband@1=tempo={options.nightcore_factor}:pitch={options.nightcore_factor}"
        )

    if options.bassboost_factor:
        filter_graph.append(f"bass@1=g={options.bassboost_factor}")

    if options.volume is not None:
        filter_graph.append(f"volume@1=volume={options.volume}")

    if options.filter_graph is not None:
        filter_graph.append(options.filter_graph)

    return ",".join(filter_graph) if len(filter_graph) > 0 else None


def ytdl_time_range(options: PlaybackOptions) -> str | None:
    if options.start_timestamp is None and options.stop_timestamp is None:
        return None

    range_opt = ""
    range_opt += (
        options.start_timestamp if options.start_timestamp is not None else "0:00"
    )
    range_opt += "-"
    range_opt += options.stop_timestamp if options.stop_timestamp is not None else "inf"

    return range_opt
