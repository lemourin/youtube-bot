import re
import subprocess
import asyncio
import json
import html
import dataclasses
from typing import Callable, Awaitable, Any
from jellyfin_apiclient_python import JellyfinClient  # type: ignore
import aiohttp
import discord


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


@dataclasses.dataclass
class MessageContent:
    title: str
    url: str | None = None
    artwork_url: str | None = None
    color: discord.Color | None = None
    author_name: str | None = None
    author_url: str | None = None
    footer: str | None = None
    description: str | None = None


@dataclasses.dataclass
class SearchEntry:
    name: str
    url: str
    on_select_message: MessageContent
    duration: int | None = None
    on_enqueue: Callable[[], Awaitable[None]] | None = None
    on_dequeue: Callable[[], Awaitable[None]] | None = None


@dataclasses.dataclass
class JellyfinLibraryClient:
    client: JellyfinClient
    library_id: str
    address: str


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


def jf_best_thumbnail_url(client: JellyfinClient, item: dict) -> str | None:
    priority = ["Primary", "Backdrop", "Logo"]

    priority_map: dict[str, int] = {}
    for index, e in enumerate(reversed(priority)):
        priority_map[e] = index

    keys = item["ImageBlurHashes"].keys()
    if len(keys) == 0:
        return None

    artwork_name = max(keys, key=lambda x: priority_map.get(x, -1))
    return client.jellyfin.artwork(
        item["AlbumId"],
        art=artwork_name,
        max_width=720,
    )


def yt_best_thumbnail_url(item: dict) -> str | None:
    return max(item["snippet"]["thumbnails"].values(), key=lambda e: e["height"])["url"]


def add_to_embed(embed: discord.Embed, options: PlaybackOptions) -> None:
    eps = 0.0001

    def remove_field(embed: discord.Embed, name: str):
        for i, field in enumerate(embed.fields):
            if field.name == name:
                embed.remove_field(i)

    def replace_field(embed: discord.Embed, name: str, value: Any):
        for i, field in enumerate(embed.fields):
            if field.name == name:
                embed.remove_field(i)
                embed.insert_field_at(i, name=name, value=value)
                return
        embed.add_field(name=name, value=value)

    if options.nightcore_factor is not None:
        if abs(1 - options.nightcore_factor) < eps:
            remove_field(embed, name="nightcore_factor")
        else:
            replace_field(
                embed, name="nightcore_factor", value=options.nightcore_factor
            )
    if options.bassboost_factor is not None:
        if abs(options.bassboost_factor) < eps:
            remove_field(embed, name="bassboost_factor")
        else:
            replace_field(
                embed, name="bassboost_factor", value=options.bassboost_factor
            )
    if options.filter_graph is not None:
        replace_field(embed, name="filter_graph", value=options.filter_graph)
    if options.start_timestamp is not None:
        replace_field(embed, name="start_timestamp", value=options.start_timestamp)
    if options.stop_timestamp is not None:
        replace_field(embed, name="stop_timestamp", value=options.stop_timestamp)
    if options.volume is not None:
        if abs(1 - options.volume) < eps:
            remove_field(embed, name="volume")
        else:
            replace_field(embed, name="volume", value=int(options.volume * 100))


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


def yt_item_to_search_item(entry: dict) -> SearchEntry:
    title = html.unescape(entry["snippet"]["title"])
    video_url = f"https://youtube.com/watch?v={entry["id"]}"
    author_url = f"https://youtube.com/channel/{entry["snippet"]["channelId"]}"
    return SearchEntry(
        name=title,
        url=video_url,
        on_select_message=MessageContent(
            title=title,
            url=video_url,
            artwork_url=yt_best_thumbnail_url(entry),
            color=discord.Color.red(),
            author_name=entry["snippet"]["channelTitle"],
            author_url=author_url,
            footer="YouTube",
        ),
        duration=iso8601_to_unix_timestamp(entry["contentDetails"]["duration"]),
    )


async def read_at_most(stream: aiohttp.streams.StreamReader, n: int) -> bytes:
    blocks: list[bytes] = []
    while n > 0 and not stream.at_eof():
        chunk = await stream.read(n)
        blocks.append(chunk)
        n -= len(chunk)
    return b"".join(blocks)
