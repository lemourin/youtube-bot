#!/usr/bin/env python
# flake8: noqa: E501
# pylint: disable=locally-disabled, missing-class-docstring, missing-module-docstring, missing-function-docstring

import asyncio
import subprocess
import threading
from typing import cast, Callable, Awaitable, Dict, Any, Sequence, Union
import os
import io
import sys
from concurrent.futures import Executor, ThreadPoolExecutor
import re
import json
import logging
import html
import aiohttp
import discord
import discord.ext.commands
from dotenv import load_dotenv
import validators
import googleapiclient.discovery
from jellyfin_apiclient_python import JellyfinClient

load_dotenv(dotenv_path=os.environ.get("ENV_FILE"))

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_BOT_COMMAND_PREFIX = os.environ.get("DISCORD_BOT_COMMAND_PREFIX", "!")
DISCORD_ADMIN_ID = int(os.environ["DISCORD_ADMIN_ID"])
HEALTHCHECK_ADDRESS = os.environ.get("HEALTHCHECK_ADDRESS")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY")
JELLYFIN_APP_NAME = os.environ.get("JELLYFIN_APP_NAME")
JELLYFIN_APP_VERSION = os.environ.get("JELLYFIN_APP_VERSION")
JELLYFIN_ADDRESS = os.environ.get("JELLYFIN_ADDRESS")
JELLYFIN_USER_ID = os.environ.get("JELLYFIN_USER_ID")
JELLYFIN_LIBRARY_ID = os.environ.get("JELLYFIN_LIBRARY_ID")
AUDIO_BITRATE = 320


class YTDLBuffer(io.BufferedIOBase):
    def __init__(self, url: str) -> None:
        self.proc: subprocess.Popen[bytes] = YTDLBuffer.__create_process(url)

    def read(self, n: int | None = None) -> bytes:
        # print("[ ] YTDLBuffer read")
        assert self.proc.stdout is not None
        return self.proc.stdout.read(-1 if n is None else n)

    def cleanup(self) -> None:
        print("[ ] YTDLBuffer cleanup")
        if self.proc is not None:
            self.proc.terminate()
            print("[ ] process cleaned up")

    @staticmethod
    def __create_process(url: str) -> subprocess.Popen[bytes]:
        print(f"[ ] YTDLBuffer creating process for {url}")
        args = [
            "-x",
            # Audio options, breaks generic downloader.
            # "-f",
            # "bestaudio",
            "--cookies",
            "cookie.txt",
            "--no-warnings",
            url,
            "-o",
            "-",
        ]
        print("[ ] yt-dlp", *args)
        return subprocess.Popen(
            executable="yt-dlp",
            args=args,
            stdout=asyncio.subprocess.PIPE,
            bufsize=0,
        )


class YTDLStreamAudio(discord.FFmpegPCMAudio):
    def __init__(self, url: str) -> None:
        self.buffer = YTDLBuffer(url)
        super().__init__(self.buffer, pipe=True, options="-vn")

    def cleanup(self) -> None:
        print("[ ] YTDLStreamAudio cleanup")
        self.buffer.cleanup()
        super().cleanup()


class LazyAudioSource(discord.AudioSource):
    def __init__(self, url: str) -> None:
        self.url = url
        self.source: discord.AudioSource | None = None

    def prefetch(self) -> None:
        if self.source is None:
            self.source = YTDLStreamAudio(self.url)

    def cleanup(self) -> None:
        if self.source:
            self.source.cleanup()

    def read(self) -> bytes:
        self.prefetch()
        assert self.source
        return self.source.read()


class YTDLQueuedStreamAudio(discord.AudioSource):
    def __init__(self) -> None:
        super().__init__()
        self.queue: list[LazyAudioSource] = []
        self.read_size = 3840
        self.zeros = b"\0" * self.read_size

    async def add(self, url: str) -> None:
        print(f"[ ] adding {url} to queue")
        self.queue.append(LazyAudioSource(url))
        if len(self.queue) == 2:
            e = self.queue[1]
            await asyncio.to_thread(e.prefetch)

    def clear(self) -> None:
        print("[ ] clearing queue")
        trash = self.queue
        self.queue = []
        for a in trash:
            a.cleanup()

    async def skip(self) -> None:
        if not self.queue:
            return
        a = self.queue[0]
        self.queue = self.queue[1:]

        if len(self.queue) > 1:
            e = self.queue[1]
            await asyncio.to_thread(e.prefetch)
        a.cleanup()

    def read(self) -> bytes:
        # print("[ ] YTDLQueuedStreamAudio read")
        trash = None
        if not self.queue:
            print("[ ] queue empty")
            return b""
        c = self.queue[0].read()
        # print(f"[ ] YTDLQueuedStreamAudio got {len(c)} bytes from queue head")
        if len(c) < self.read_size:
            if len(self.queue) > 1:
                c = c + self.zeros[len(c) :]
            trash = self.queue[0]
            print("[ ] advancing queue")
            self.queue = self.queue[1:]
            if len(self.queue) > 1:
                self.queue[1].prefetch()
        if trash is not None:
            trash.cleanup()
        return c

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        print("[ ] YTDLQueuedStreamAudio cleanup")
        trash = self.queue
        self.queue = []
        for a in trash:
            cast(YTDLStreamAudio, a).cleanup()


class BufferedAudioSource(discord.AudioSource):
    def __init__(self, source: discord.AudioSource, executor: Executor) -> None:
        self.done = False
        self.max_chunk_count = 256
        self.preload_chunk_count = 128
        self.source = source
        self.executor = executor
        self.chunks: list[bytes] = []
        self.access_sem = threading.Lock()
        self.chunk_sem = threading.Semaphore(value=0)
        self.cv = threading.Condition(self.access_sem)
        self.future = self.executor.submit(self.__fetcher_task)
        with self.chunk_sem:
            pass

    def drain(self) -> None:
        with self.access_sem:
            self.chunks.clear()
            self.cv.notify()

    def read(self) -> bytes:
        # print("[ ] BufferedAudioSource read")
        self.chunk_sem.acquire()
        with self.access_sem:
            if not self.chunks:
                print("[ ] BufferedAudioSource finished")
                self.chunk_sem.release()
                return b""
            c = self.chunks[0]
            self.chunks = self.chunks[1:]
            if len(self.chunks) == self.max_chunk_count - 1:
                self.cv.notify()
            return c

    def cleanup(self) -> None:
        print("[ ] BufferedAudioSource cleanup")
        if not self.future:
            return
        with self.access_sem:
            self.done = True
            self.cv.notify()
        self.future.result()

    def __fetcher_task(self) -> None:
        chunks_pending = 0
        while True:
            with self.access_sem:
                self.cv.wait_for(
                    lambda: len(self.chunks) < self.max_chunk_count or self.done
                )
                if self.done:
                    print("[ ] BufferedAudioSource fetcher stopped, close event")
                    self.chunk_sem.release(chunks_pending + 1)
                    break
                if len(self.chunks) >= self.preload_chunk_count:
                    chunks_pending -= 1
                    self.chunk_sem.release()

            data = self.source.read()
            if len(data) == 0:
                self.chunk_sem.release(chunks_pending + 1)
                print("[ ] BufferedAudioSource fetcher stopped, finished")
                break
            with self.access_sem:
                self.chunks.append(data)
            chunks_pending += 1


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(
        self, queue: discord.AudioSource, volume: float, executor: Executor
    ) -> None:
        self.buffered_audio = BufferedAudioSource(queue, executor)
        super().__init__(self.buffered_audio, volume)


class SelectTrack(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__()
        self._callback: Callable[[discord.Interaction], Awaitable[None]] | None = None

    def selected_value(self) -> str:
        return self.values[0]

    def set_callback(self, callback: Callable[[discord.Interaction], Awaitable[None]]):
        self._callback = callback

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self._callback
        return await self._callback(interaction)


class GuildState:
    def __init__(self, executor: Executor) -> None:
        self._executor = executor
        self._queue = YTDLQueuedStreamAudio()
        self._source: YTDLSource | None = None
        self._is_playing = False
        self._volume = 0.1

    async def enqueue(self, voice_client: discord.VoiceClient, url: str) -> None:
        await self._queue.add(url)
        if self._is_playing:
            return
        print("[ ] voice client not playing, starting")
        self._is_playing = True
        self._source = await asyncio.to_thread(
            lambda: YTDLSource(
                self._queue,
                self._volume,
                self._executor,
            )
        )

        def finalizer(self, err):
            if err:
                print(f"[!] player error: {err}")
            else:
                print("[ ] finished playing")
                self._is_playing = False

        voice_client.play(
            self._source,
            signal_type="music",
            bitrate=AUDIO_BITRATE,
            fec=False,
            # fec=True,
            # expected_packet_loss=0.05,
            after=lambda err: finalizer(self, err),
        )

        print("[ ] play started")

    async def skip(self) -> None:
        if self._source is not None and self._source.buffered_audio:
            self._source.buffered_audio.drain()
        if self._queue:
            await self._queue.skip()

    def is_playing(self) -> bool:
        return self._is_playing

    def cleanup(self) -> None:
        if self._source is not None:
            self._source.cleanup()
        self._queue.cleanup()
        self._queue.clear()

    def set_volume(self, value: float) -> None:
        self._volume = value


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

    artwork_name = max(
        item["ImageBlurHashes"].keys(),
        key=lambda x: priority_map.get(x, -1),
    )
    return client.jellyfin.artwork(
        item["AlbumId"],
        art=artwork_name,
        max_width=720,
    )


class MessageContent:
    def __init__(
        self,
        content: str | None = discord.utils.MISSING,
        artwork_url: str | None = None,
    ):
        self.content = content
        self.artwork_url = artwork_url


class SearchEntry:
    def __init__(
        self,
        name: str,
        url: str,
        on_select_message: MessageContent,
        duration: int | None = None,
    ) -> None:
        self.name = name
        self.url = url
        self.on_select_message = on_select_message
        self.duration = duration


class Audio(discord.ext.commands.Cog):
    def __init__(
        self,
        bot: discord.ext.commands.Bot,
        executor: Executor,
        http: aiohttp.ClientSession,
        jellyfin_client: JellyfinClient | None = None,
        youtube_client: Any = None,
    ) -> None:
        self.bot = bot
        self.executor = executor
        self.http = http
        self.state: Dict[int, GuildState] = {}
        self.jellyfin_client = jellyfin_client
        self.youtube_client = youtube_client

        @bot.event
        async def on_ready() -> None:
            assert bot.user is not None
            print(f"[ ] logged in {bot.user} {bot.user.id}")

        @bot.event
        async def on_voice_state_update(
            member: discord.Member,
            before: discord.VoiceState,
            after: discord.VoiceState,
        ) -> None:
            assert bot.user is not None
            if (
                member.id == bot.user.id
                and member.guild.id in self.state
                and not after.channel
            ):
                self.state[member.guild.id].cleanup()
                del self.state[member.guild.id]

            if before.channel is None:
                return

            if len(before.channel.members) != 1:
                return
            bot_member = before.channel.members[0]
            if bot_member.id != bot.user.id:
                return
            if bot_member.guild.voice_client is not None:
                await bot_member.guild.voice_client.disconnect(force=False)

        for command in [
            discord.app_commands.Command(
                name="yt",
                description="Play audio of a YouTube video.",
                callback=self.yt,
            ),
            discord.app_commands.Command(
                name="jf",
                description="Play audio of a video sourced from a Jellyfin server.",
                callback=self.jf,
            ),
            discord.app_commands.Command(
                name="join",
                description="Add bot to the voice channel.",
                callback=self.join,
            ),
            discord.app_commands.Command(
                name="skip",
                description="Skip currently playing audio.",
                callback=self.skip,
            ),
            discord.app_commands.Command(
                name="volume",
                description="Change volume of the currently playing audio.",
                callback=self.volume,
            ),
            discord.app_commands.Command(
                name="stop",
                description="Pause audio playback.",
                callback=self.stop,
            ),
            discord.app_commands.Command(
                name="leave",
                description="Disconnect bot from the voice channel.",
                callback=self.leave,
            ),
            discord.app_commands.Command(
                name="die",
                description="Kill the bot.",
                callback=self.die,
            ),
            discord.app_commands.Command(
                name="ping",
                description="Ping.",
                callback=self.ping,
            ),
        ]:
            bot.tree.add_command(cast(discord.app_commands.Command, command))

    @discord.app_commands.describe(query="Either a url or a search query.")
    async def yt(self, interaction: discord.Interaction, query: str) -> None:
        print("[ ] yt app command")
        if validators.url(query):
            await self.__enqueue(await self.__voice_client(interaction), query)
            await interaction.response.send_message(
                query, ephemeral=True, delete_after=5
            )
            return

        if not self.youtube_client:
            await interaction.response.send_message(
                "YouTube search not set up.", ephemeral=True
            )
            return

        response = await asyncio.to_thread(
            self.youtube_client.search()
            .list(part="snippet", maxResults=10, type=["video"], q=query)
            .execute
        )

        details = await asyncio.to_thread(
            self.youtube_client.videos()
            .list(
                part="snippet, contentDetails",
                id=",".join([e["id"]["videoId"] for e in response["items"]]),
            )
            .execute
        )

        entries: list[SearchEntry] = []
        for entry in details["items"]:
            if len(entries) >= 10:
                break
            if entry["kind"] == "youtube#video":
                video_url = f"https://youtube.com/watch?v={entry["id"]}"
                entries.append(
                    SearchEntry(
                        name=html.unescape(entry["snippet"]["title"]),
                        url=video_url,
                        on_select_message=MessageContent(content=video_url),
                        duration=iso8601_to_unix_timestamp(
                            entry["contentDetails"]["duration"]
                        ),
                    )
                )
        await self.__search_result_select(interaction, entries)

    @discord.app_commands.describe(query="Search query.")
    async def jf(self, interaction: discord.Interaction, query: str) -> None:
        if self.jellyfin_client is None:
            await interaction.response.send_message(
                "Jellyfin not set up.", ephemeral=True
            )
            return

        jellyfin_client = self.jellyfin_client

        print(f"[ ] jf {query}")
        result = await asyncio.to_thread(
            self.jellyfin_client.jellyfin.search_media_items,
            media="Audio",
            term=query,
            parent_id=JELLYFIN_LIBRARY_ID,
        )

        entries: list[SearchEntry] = []
        for entry in result["Items"]:
            if len(entries) >= 10:
                break
            if entry["Type"] != "Audio":
                continue
            artist_name = entry["Artists"][0] if entry["Artists"] else "Unknown Artist"
            name = f"{artist_name} - {entry["Name"]}"

            entries.append(
                SearchEntry(
                    name=name,
                    url=jellyfin_client.jellyfin.download_url(entry["Id"]),
                    on_select_message=MessageContent(
                        content=name,
                        artwork_url=jf_best_thumbnail_url(self.jellyfin_client, entry),
                    ),
                    duration=entry["RunTimeTicks"] // 10_000_000,
                )
            )
        await self.__search_result_select(interaction, entries)

    async def __search_result_select(
        self, interaction: discord.Interaction, entries: list[SearchEntry]
    ) -> None:
        if not entries:
            await interaction.response.send_message(
                "No results.", ephemeral=True, delete_after=5
            )
            return

        def option_label(index: int, entry: SearchEntry) -> str:
            message = f"{index + 1}. {entry.name}"
            return trim_option_text(message)

        select = SelectTrack()
        message = ""
        for index, entry in enumerate(entries):
            print(option_label(index, entry))
            message += f"{option_label(index, entry)}"
            if entry.duration:
                message += f" - {duration_to_str(entry.duration)}\n"
            else:
                message += "\n"
            select.add_option(
                label=option_label(index, entry),
                description=(
                    duration_to_str(entry.duration) if entry.duration else None
                ),
            )

        view = discord.ui.View()
        view.add_item(select)

        dismissed = False

        async def on_selected(selection_interaction: discord.Interaction):
            if interaction.user.id != selection_interaction.user.id:
                await selection_interaction.response.send_message(
                    "Fuck off.", ephemeral=True, delete_after=5
                )
                return
            nonlocal dismissed
            dismissed = True
            try:
                item = [
                    entry
                    for index, entry in enumerate(entries)
                    if option_label(index, entry) == select.selected_value()
                ][0]
                await self.__enqueue(
                    await self.__voice_client(selection_interaction), item.url
                )
                embed: discord.Embed | None = discord.utils.MISSING
                attachments: Sequence[discord.File] = []
                if item.on_select_message.artwork_url:
                    async with await self.http.get(
                        item.on_select_message.artwork_url
                    ) as image:
                        if image.ok:
                            content = await image.content.read()
                            attachments = [
                                discord.File(
                                    io.BytesIO(content),
                                    filename="artwork.jpg",
                                )
                            ]
                            embed = discord.Embed()
                            embed.set_image(url="attachment://artwork.jpg")

                await interaction.edit_original_response(
                    content=item.on_select_message.content,
                    embed=embed,
                    attachments=attachments,
                    view=None,
                )
            except discord.DiscordException as e:
                print(f"[ ] Interaction error: {e}")
                dismissed = False
                await interaction.edit_original_response(view=view)
                return

        select.set_callback(on_selected)
        await interaction.response.send_message(
            f"```{message}```",
            view=view,
        )
        await asyncio.sleep(10)
        if not dismissed:
            await interaction.delete_original_response()

    @discord.ext.commands.command()
    async def sync(self, ctx: discord.ext.commands.Context) -> None:
        print("[ ] sync")
        assert ctx.guild is not None

        guild = discord.Object(id=ctx.guild.id)
        self.bot.tree.copy_global_to(guild=guild)
        await self.bot.tree.sync(guild=guild)

    async def join(self, interaction: discord.Interaction) -> None:
        print("[ ] join")
        await self.__ensure_voice(interaction)
        author = cast(discord.Member, interaction.user)
        assert author.voice is not None
        await (await self.__voice_client(interaction)).move_to(author.voice.channel)
        await interaction.response.send_message("Joined.", ephemeral=True)

    async def skip(self, interaction: discord.Interaction) -> None:
        print("[ ] skip")
        await self.__ensure_playing(interaction)
        assert interaction.guild
        await self.state[interaction.guild.id].skip()
        await interaction.response.send_message("Skipped.", ephemeral=True)

    @discord.app_commands.describe(volume="Number from 0 to 200.")
    async def volume(self, interaction: discord.Interaction, volume: int) -> None:
        await self.__ensure_playing(interaction)
        print(f"[ ] volume {volume}")
        volume = max(min(volume, 200), 0)
        voice_client = await self.__voice_client(interaction)

        assert interaction.guild
        state = self.__guild_state(interaction.guild.id)
        state.set_volume(volume / 100)
        cast(YTDLSource, voice_client.source).volume = volume / 100
        await interaction.response.send_message(
            f"volume set to {volume}%", ephemeral=True
        )

    async def stop(self, interaction: discord.Interaction) -> None:
        print("[ ] stop")
        await self.__ensure_playing(interaction)
        for e in self.bot.voice_clients:
            voice_client = cast(discord.VoiceClient, e)
            if voice_client.guild.id == interaction.guild_id:
                if voice_client.source is not None:
                    voice_client.stop()

    async def leave(self, interaction: discord.Interaction) -> None:
        print("[ ] leave")
        voice_client = self.__current_voice_client(interaction)
        if voice_client is not None:
            await voice_client.disconnect(force=False)
        await interaction.response.send_message("Left.", ephemeral=True)

    async def die(self, interaction: discord.Interaction) -> None:
        print("[ ] die")
        await interaction.response.send_message("About to die.", ephemeral=True)
        sys.exit(0)

    async def ping(self, interaction: discord.Interaction) -> None:
        print("[ ] ping")
        await interaction.response.send_message("pong", ephemeral=True)

    async def __ensure_voice(self, interaction: discord.Interaction) -> None:
        member = cast(discord.Member, interaction.user)
        if member.voice is None:
            await interaction.response.send_message(
                "no voice channel, dumbass", ephemeral=True
            )
            raise discord.ext.commands.CommandError("not connected to a voice channel")

    async def __ensure_playing(self, interaction: discord.Interaction) -> None:
        await self.__ensure_voice(interaction)
        assert interaction.guild
        if (
            interaction.guild.id not in self.state
            or not self.state[interaction.guild.id].is_playing()
        ):
            await interaction.response.send_message(
                "not playing, dumbass", ephemeral=True
            )
            raise discord.ext.commands.CommandError("audio not playing")

    def __current_voice_client(
        self, interaction: discord.Interaction
    ) -> discord.VoiceClient | None:
        for e in self.bot.voice_clients:
            voice_client = cast(discord.VoiceClient, e)
            if voice_client.guild.id == interaction.guild_id:
                return voice_client
        return None

    async def __voice_client(
        self, interaction: discord.Interaction
    ) -> discord.VoiceClient:
        voice_client = self.__current_voice_client(interaction)
        if voice_client:
            return voice_client
        assert isinstance(interaction.user, discord.Member)
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.response.send_message(
                "no voice channel, dumbass", ephemeral=True
            )
            raise discord.ext.commands.CommandError("not connected to a voice channel")
        return await interaction.user.voice.channel.connect()

    def __guild_state(self, guild_id: int) -> GuildState:
        if guild_id in self.state:
            return self.state[guild_id]
        state = GuildState(self.executor)
        self.state[guild_id] = state
        return state

    async def __enqueue(self, voice_client: discord.VoiceClient, url: str) -> None:
        state = self.__guild_state(voice_client.guild.id)
        await state.enqueue(voice_client, url)


async def healthcheck(http: aiohttp.ClientSession) -> None:
    if not HEALTHCHECK_ADDRESS or len(HEALTHCHECK_ADDRESS) == 0:
        return
    while True:
        try:
            async with http.get(HEALTHCHECK_ADDRESS) as response:
                await response.text()
        except aiohttp.web_exceptions.HTTPException as e:
            print(f"[ ] health check error {e}")
        await asyncio.sleep(60)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    intents = discord.Intents.default()
    intents.message_content = True

    bot = discord.ext.commands.Bot(
        command_prefix=discord.ext.commands.when_mentioned_or(
            DISCORD_BOT_COMMAND_PREFIX
        ),
        intents=intents,
    )

    jellyfin_client: JellyfinClient | None = None
    if JELLYFIN_ADDRESS:
        jellyfin_client = JellyfinClient()
        jellyfin_client.config.data["app.name"] = JELLYFIN_APP_NAME
        jellyfin_client.config.data["app.version"] = JELLYFIN_APP_VERSION
        jellyfin_client.config.data["auth.ssl"] = True
        jellyfin_client.authenticate(
            {
                "Servers": [
                    {
                        "AccessToken": JELLYFIN_API_KEY,
                        "address": JELLYFIN_ADDRESS,
                        "UserId": JELLYFIN_USER_ID,
                    }
                ]
            },
            discover=False,
        )
    youtube_client = None
    if YOUTUBE_API_KEY:
        youtube_client = googleapiclient.discovery.build(
            "youtube",
            "v3",
            developerKey=YOUTUBE_API_KEY,
        )

    async with bot, aiohttp.ClientSession() as http_session:
        with ThreadPoolExecutor(max_workers=32) as executor:
            await bot.add_cog(
                Audio(bot, executor, http_session, jellyfin_client, youtube_client)
            )
            await asyncio.gather(
                bot.start(DISCORD_BOT_TOKEN), healthcheck(http_session)
            )


if __name__ == "__main__":
    asyncio.run(main())
