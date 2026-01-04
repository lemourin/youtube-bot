import discord
import html
from .util import PlaybackOptions, iso8601_to_unix_timestamp, yt_best_thumbnail_url
from typing import Any, Callable, Awaitable
import dataclasses


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


def add_to_embed(embed: discord.Embed, options: PlaybackOptions) -> None:
    if embed.image.url and embed.image.url.startswith(
        "https://cdn.discordapp.com/attachments/"
    ):
        embed.set_image(url="attachment://artwork.jpg")

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
