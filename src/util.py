import re
from jellyfin_apiclient_python import JellyfinClient  # type: ignore
import discord
from src.audio import PlaybackOptions


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
    if options.nightcore_factor:
        embed.add_field(name="nightcore_factor", value=options.nightcore_factor)
    if options.bassboost_factor:
        embed.add_field(name="bassboost_factor", value=options.bassboost_factor)
    if options.filter_graph:
        embed.add_field(name="filter_graph", value=options.filter_graph)
