import asyncio
from typing import cast, Dict, Any, Tuple, IO
import io
import sys
import functools
from urllib.parse import urlparse
from concurrent.futures import Executor
import subprocess
import dataclasses
import aiohttp
import discord
import discord.ext.commands
import validators
import yt_dlp  # type: ignore
from linkpreview import link_preview  # type: ignore
from src.audio import YTDLSource, AudioTrack
from src.guild_state import GuildState
from src.discord_ui import ButtonView, SelectView, View
from src.util import (
    read_at_most,
    yt_video_data_from_url,
    yt_item_to_search_item,
    jf_best_thumbnail_url,
    trim_option_text,
    add_to_embed,
    duration_to_str,
    JellyfinLibraryClient,
    SearchEntry,
    MessageContent,
    PlaybackOptions,
)

MAX_SIZE = 10_000_000
PAGE_SIZE = 4096


class DiscordCog(discord.ext.commands.Cog):
    def __init__(
        self,
        bot: discord.ext.commands.Bot,
        executor: Executor,
        http: aiohttp.ClientSession,
        discord_admin_id: int,
        jellyfin_client: JellyfinLibraryClient | None = None,
        youtube_client: Any = None,
    ) -> None:
        self.bot = bot
        self.executor = executor
        self.http = http
        self.discord_admin_id = discord_admin_id
        self.state: Dict[int, GuildState] = {}
        self.jellyfin_client = jellyfin_client
        self.youtube_client = youtube_client
        self.next_track_id = 0

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
                await self.state[member.guild.id].cleanup()
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
            discord.app_commands.Command(
                name="queue",
                description="Show enqueued tracks.",
                callback=self.queue,
            ),
            discord.app_commands.Command(
                name="attach",
                description="Extract a video out of a url and post an embed with it.",
                callback=self.attach,
            ),
            discord.app_commands.Command(
                name="set",
                description="Alter properties of the currently playing audio.",
                callback=self.set,
            ),
            discord.app_commands.Command(
                name="nightcore",
                description="Apply nightcore filter to currently playing audio.",
                callback=self.nightcore,
            ),
            discord.app_commands.Command(
                name="bassboost",
                description="Apply bassboost filter to currently playing audio.",
                callback=self.bassboost,
            ),
            discord.app_commands.Command(
                name="denightcore",
                description="Negate nightcore filter applied to currently playing audio.",
                callback=self.denightcore,
            ),
            discord.app_commands.Command(
                name="debassboost",
                description="Negate bassboost filter applied to currently playing audio.",
                callback=self.debassboost,
            ),
        ]:
            bot.tree.add_command(cast(discord.app_commands.Command, command))

    async def __url(
        self, interaction: discord.Interaction, url: str, options: PlaybackOptions
    ) -> None:
        assert interaction.guild_id
        track_id = self.next_track_id
        self.next_track_id += 1
        state = self.__guild_state(interaction.guild_id)
        voice_client = await state.voice_client(interaction)
        await interaction.response.defer()

        async def message_content_with_yt_dlp() -> MessageContent | None:
            details = await asyncio.to_thread(yt_video_data_from_url, url)
            if details is None or "title" not in details:
                return None
            return MessageContent(
                title=details["title"],
                artwork_url=details.get("thumbnail"),
                author_name=details.get("uploader"),
                author_url=details.get("uploader_url"),
                color=discord.Color.red(),
                url=details.get("original_url"),
            )

        async def message_content_with_link_preview() -> MessageContent | None:
            try:
                async with self.http.get(url) as data:
                    if data.content_type != "text/html":
                        return None
                    preview = await asyncio.to_thread(
                        link_preview,
                        url=url,
                        content=await read_at_most(data.content, 1024 * 1024),
                    )
                    return MessageContent(
                        title=preview.title,
                        artwork_url=preview.image,
                        url=url,
                        footer=preview.site_name,
                        description=preview.description,
                    )
            except aiohttp.web.HTTPException as e:
                print(f"[ ] preview error {e}")
            return None

        async def create_message_content() -> MessageContent | None:
            m1, m2 = await asyncio.gather(
                message_content_with_yt_dlp(), message_content_with_link_preview()
            )
            if not m1:
                return m2
            if not m2:
                return m1
            m1.artwork_url = m1.artwork_url or m2.artwork_url
            m1.author_name = m1.author_name or m2.author_name
            m1.author_url = m1.author_url or m2.author_url
            m1.url = m1.url or m2.url
            m1.footer = m1.footer or m2.footer
            m1.description = m1.description or m2.description
            return m1

        message_content = await create_message_content()
        if message_content is None:
            message_content = MessageContent(title=url, url=url)
        if message_content.footer is None:
            message_content.footer = urlparse(url).hostname

        track = AudioTrack(
            url=url,
            title=message_content.title,
            track_id=track_id,
            playback_options=options,
            interaction=interaction,
            can_edit_message=False,
        )

        await state.enqueue(voice_client, track)

        embed, attachments = await self.__create_embed(
            message_content,
            options,
        )
        await interaction.followup.send(
            embed=embed,
            files=attachments,
            view=state.new_playback_control_view(
                interaction,
                track,
            ),
        )
        track.can_edit_message = True

    @discord.app_commands.describe(
        query="Either a url or a search query.",
        nightcore_factor=PlaybackOptions.NIGHTCORE_FACTOR_DOC,
        bassboost_factor=PlaybackOptions.BASSBOOST_FACTOR_DOC,
        filter_graph=PlaybackOptions.FILTER_GRAPH_DOC,
        start_timestamp=PlaybackOptions.START_TIMESTAMP_DOC,
        stop_timestamp=PlaybackOptions.STOP_TIMESTAMP_DOC,
        volume=PlaybackOptions.VOLUME_DOC,
    )
    async def yt(
        self,
        interaction: discord.Interaction,
        query: str,
        nightcore_factor: float | None,
        bassboost_factor: float | None,
        filter_graph: str | None,
        start_timestamp: str | None,
        stop_timestamp: str | None,
        volume: int | None,
    ) -> None:
        print("[ ] yt app command")
        await self.__authorize_options(interaction, filter_graph)
        options = PlaybackOptions(
            nightcore_factor=nightcore_factor,
            bassboost_factor=bassboost_factor,
            filter_graph=filter_graph,
            start_timestamp=start_timestamp,
            stop_timestamp=stop_timestamp,
            volume=min(max(volume, 0), 200) / 100 if volume else None,
        )
        if validators.url(query):
            return await self.__url(interaction, url=query, options=options)

        if not self.youtube_client:
            await interaction.response.send_message(
                "YouTube search not set up.", ephemeral=True, delete_after=5
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
                id=",".join(
                    [
                        e["id"]["videoId"]
                        for e in response["items"]
                        if "videoId" in e["id"]
                    ]
                ),
            )
            .execute
        )

        entries: list[SearchEntry] = []
        for entry in details["items"]:
            if len(entries) >= 10:
                break
            if entry["kind"] == "youtube#video":
                entries.append(yt_item_to_search_item(entry))
        await self.__search_result_select(interaction, entries, options)

    async def _jf_query(self, query: str) -> list[dict]:
        assert self.jellyfin_client
        result = await asyncio.to_thread(
            self.jellyfin_client.client.jellyfin.search_media_items,
            term=query,
            parent_id=self.jellyfin_client.library_id,
            limit=200,
        )

        items: list[dict] = []
        coros = []
        for e in result["Items"]:
            if e["Type"] == "Audio":
                items.append(e)
            if e["Type"] == "MusicArtist":
                coros.append(
                    asyncio.to_thread(
                        self.jellyfin_client.client.jellyfin.search_media_items,
                        media="Audio",
                        parent_id=e["Id"],
                        limit=200,
                    )
                )
        for d in await asyncio.gather(*coros):
            for e in d["Items"]:
                items.append(e)

        ids = set()
        filtered = []
        for i in items:
            if i["Id"] not in ids:
                ids.add(i["Id"])
                print(f"{i["Name"]} - {i["UserData"]["PlayCount"]}")
                filtered.append(i)
        filtered.sort(key=lambda x: x["UserData"]["PlayCount"], reverse=True)
        return filtered

    @discord.app_commands.describe(
        query="Search query.",
        nightcore_factor=PlaybackOptions.NIGHTCORE_FACTOR_DOC,
        bassboost_factor=PlaybackOptions.BASSBOOST_FACTOR_DOC,
        filter_graph=PlaybackOptions.FILTER_GRAPH_DOC,
        start_timestamp=PlaybackOptions.START_TIMESTAMP_DOC,
        stop_timestamp=PlaybackOptions.STOP_TIMESTAMP_DOC,
        volume=PlaybackOptions.VOLUME_DOC,
    )
    async def jf(
        self,
        interaction: discord.Interaction,
        query: str,
        nightcore_factor: float | None,
        bassboost_factor: float | None,
        filter_graph: str | None,
        start_timestamp: str | None,
        stop_timestamp: str | None,
        volume: int | None,
    ) -> None:
        print(f"[ ] jf {query}")
        await self.__authorize_options(interaction, filter_graph)

        if self.jellyfin_client is None:
            await interaction.response.send_message(
                "Jellyfin not set up.", ephemeral=True, delete_after=5
            )
            return

        client = self.jellyfin_client.client.jellyfin
        entries: list[SearchEntry] = []
        for entry in await self._jf_query(query):
            if len(entries) >= 10:
                break
            artist_name = entry["Artists"][0] if entry["Artists"] else "Unknown Artist"
            name = f"{artist_name} - {entry["Name"]}"

            async def on_enqueue(entry_id: str) -> None:
                await asyncio.to_thread(
                    client.session_playing,
                    {
                        "ItemId": entry_id,
                        "MediaSourceId": entry_id,
                    },
                )

            async def on_dequeue(entry_id: str) -> None:
                await asyncio.to_thread(
                    client.session_stop,
                    {
                        "ItemId": entry_id,
                        "MediaSourceId": entry_id,
                        "PositionTicks": 0,
                    },
                )

            entries.append(
                SearchEntry(
                    name=name,
                    url=self.jellyfin_client.client.jellyfin.audio_url(
                        entry["Id"], container="flac", audio_codec="flac"
                    ),
                    on_select_message=MessageContent(
                        title=entry["Name"],
                        artwork_url=jf_best_thumbnail_url(
                            self.jellyfin_client.client, entry
                        ),
                        author_name=artist_name,
                        color=discord.Color.blue(),
                        footer="Jellyfin",
                    ),
                    duration=entry["RunTimeTicks"] // 10_000_000,
                    on_enqueue=functools.partial(on_enqueue, entry["Id"]),
                    on_dequeue=functools.partial(on_dequeue, entry["Id"]),
                )
            )
        await self.__search_result_select(
            interaction,
            entries,
            PlaybackOptions(
                nightcore_factor=nightcore_factor,
                bassboost_factor=bassboost_factor,
                filter_graph=filter_graph,
                start_timestamp=start_timestamp,
                stop_timestamp=stop_timestamp,
                volume=min(max(volume, 0), 200) / 100 if volume else None,
            ),
        )

    @discord.app_commands.describe(
        url="A url to extract a video from.",
    )
    async def attach(self, interaction: discord.Interaction, url: str) -> None:
        format_str = "b[filesize<8M][vcodec!*=av01]/bv[filesize<6M][vcodec!*=av01]+ba[filesize<2M]/b[vcodec!*=av01]/bv[vcodec!*=av01]+ba/b/bv+ba/bv[vcodec!*=av01]/bv/ba"

        await interaction.response.defer()

        @dataclasses.dataclass
        class InlineAttachment:
            content: bytes
            ext: str

        @dataclasses.dataclass
        class Attachment:
            url: str | None
            title: str
            inline_attachment: InlineAttachment | None = None

        def extract_content() -> Attachment:
            with yt_dlp.YoutubeDL(
                params={
                    "format": format_str,
                    "format_sort": ["+size", "+br"],
                    "cookiefile": "cookie.txt",
                }
            ) as yt:
                info = yt.extract_info(url, download=False)
                duration_seconds = info.get("duration")
                ext = info["ext"] if info["ext"] != "mkv" else "webm"

            attachment = Attachment(url=info.get("url"), title=info["title"])

            def read(stream: IO[bytes]) -> bytes:
                chunks: list[bytes] = []
                size = 0
                while True:
                    chunk = stream.read(PAGE_SIZE)
                    if len(chunk) == 0:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_SIZE:
                        raise discord.ext.commands.CommandError(
                            f"Attachment too large ({size / (2**20)} Mb)!"
                        )
                print(f"[ ] attachment size = {size}")
                return b"".join(chunks)

            if not duration_seconds:
                return attachment
            audio_bitrate = 64000
            video_bitrate = 6 * MAX_SIZE / duration_seconds - audio_bitrate
            if video_bitrate < 1000:
                return attachment

            with subprocess.Popen(
                args=(
                    [
                        "yt-dlp",
                        "--cookies",
                        "cookie.txt",
                        url,
                        "-f",
                        format_str,
                        "-o",
                        "-",
                    ]
                    + (
                        [
                            "--external-downloader-args",
                            "ffmpeg:-f mp4 -movflags frag_keyframe+empty_moov",
                        ]
                        if ext == "mp4"
                        else []
                    )
                ),
                stdout=asyncio.subprocess.PIPE,
                bufsize=0,
            ) as input_data:
                with subprocess.Popen(
                    args=[
                        "ffmpeg",
                        "-i",
                        "-",
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        "-b:v",
                        f"{video_bitrate}",
                        "-maxrate",
                        f"{video_bitrate}",
                        "-bufsize",
                        "1M",
                        "-c:a",
                        "aac",
                        "-b:a",
                        f"{audio_bitrate}",
                        "-f",
                        "mp4",
                        "-movflags",
                        "frag_keyframe+empty_moov",
                        "-",
                    ],
                    stdin=input_data.stdout,
                    stdout=asyncio.subprocess.PIPE,
                    bufsize=0,
                ) as postproc:
                    try:
                        assert postproc.stdout
                        attachment.inline_attachment = InlineAttachment(
                            content=read(postproc.stdout),
                            ext="mp4",
                        )
                        return attachment
                    except discord.ext.commands.CommandError as e:
                        print(f"[ ] failed to inline attachment: {e}")
                        return attachment

        try:
            attachment = await asyncio.to_thread(extract_content)
            if attachment.inline_attachment is not None:
                await interaction.followup.send(
                    content=f"## {attachment.title}",
                    file=discord.File(
                        io.BytesIO(attachment.inline_attachment.content),
                        filename=f"file.{attachment.inline_attachment.ext}",
                    ),
                )
            elif attachment.url is not None:
                await interaction.followup.send(content=attachment.url)
            else:
                await interaction.followup.send("Failed to extract a video.")
        except discord.ext.commands.CommandError as e:
            await interaction.followup.send(e.args[0])
        except yt_dlp.utils.YoutubeDLError:
            await interaction.followup.send("Failed to extract a video.")

    async def __authorize_options(
        self, interaction: discord.Interaction, filter_graph: str | None
    ):
        if interaction.user.id != self.discord_admin_id and filter_graph:
            await interaction.response.send_message(
                "Not allowed to use the filter_graph option.",
                ephemeral=True,
                delete_after=5,
            )
            raise discord.ext.commands.CommandError(
                "Not authorized to use filter_graph option."
            )

    def __create_embed_ui(
        self,
        message_content: MessageContent,
        options: PlaybackOptions,
        image: io.BytesIO | str | None,
    ) -> Tuple[discord.Embed, list[discord.File]]:
        embed = discord.Embed(
            title=message_content.title,
            url=message_content.url,
            color=message_content.color,
        )
        if message_content.author_name is not None:
            embed.set_author(
                name=message_content.author_name,
                url=message_content.author_url,
            )
        if message_content.footer is not None:
            embed.set_footer(text=message_content.footer)
        add_to_embed(embed, options)
        files: list[discord.File] = []
        if isinstance(image, io.BytesIO):
            embed.set_image(url="attachment://artwork.jpg")
            files = [
                discord.File(
                    image,
                    filename="artwork.jpg",
                )
            ]
        elif isinstance(image, str):
            embed.set_image(url=image)
        return embed, files

    async def __create_embed(
        self, message_content: MessageContent, options: PlaybackOptions
    ) -> Tuple[discord.Embed, list[discord.File]]:
        image: io.BytesIO | None = None
        if (
            message_content.artwork_url
            and self.jellyfin_client
            and message_content.artwork_url.startswith(self.jellyfin_client.address)
        ):
            async with await self.http.get(message_content.artwork_url) as response:
                if response.ok:
                    image = io.BytesIO(
                        await read_at_most(response.content, 10 * 1024 * 1024)
                    )
                    return self.__create_embed_ui(message_content, options, image)
        return self.__create_embed_ui(
            message_content, options, image=message_content.artwork_url
        )

    async def __search_result_select(
        self,
        interaction: discord.Interaction,
        entries: list[SearchEntry],
        options: PlaybackOptions,
    ) -> None:
        if not entries:
            await interaction.response.send_message(
                "No results.", ephemeral=True, delete_after=5
            )
            return

        def option_label(index: int, entry: SearchEntry) -> str:
            return f"{index + 1}. {entry.name}"

        dismissed = False
        dismissed_lock = asyncio.Lock()

        async def on_timeout():
            nonlocal dismissed
            async with dismissed_lock:
                if not dismissed:
                    await interaction.delete_original_response()
                    dismissed = True

        view = View(on_timeout=on_timeout, timeout=30)

        async def on_selected(selection_interaction: discord.Interaction) -> None:
            nonlocal dismissed
            if interaction.user.id != selection_interaction.user.id:
                await selection_interaction.response.send_message(
                    "Fuck off.", ephemeral=True, delete_after=5
                )
                return
            async with dismissed_lock:
                await selection_interaction.response.defer()

                if dismissed:
                    return

                item = [
                    entry
                    for index, entry in enumerate(entries)
                    if trim_option_text(option_label(index, entry))
                    == select.selected_value()
                ][0]

                track_id = self.next_track_id
                self.next_track_id += 1

                track = AudioTrack(
                    url=item.url,
                    title=item.name,
                    track_id=track_id,
                    playback_options=options,
                    interaction=interaction,
                    on_enqueue=item.on_enqueue,
                    on_dequeue=item.on_dequeue,
                )

                try:
                    assert selection_interaction.guild_id
                    state = self.__guild_state(selection_interaction.guild_id)
                    voice_client = await state.voice_client(selection_interaction)
                    await state.enqueue(voice_client, track)
                    embed, attachments = await self.__create_embed(
                        item.on_select_message, options
                    )
                    await selection_interaction.edit_original_response(
                        content=None,
                        embed=embed,
                        attachments=attachments,
                        view=state.new_playback_control_view(interaction, track),
                    )
                    dismissed = True
                except discord.DiscordException as e:
                    print(f"[ ] interaction error: {e}")
                    await selection_interaction.edit_original_response(view=view)

        select = SelectView(callback=on_selected)
        message = ""
        for index, entry in enumerate(entries):
            print(f"[ ] search result: {option_label(index, entry)}")
            message += f"{option_label(index, entry)}"
            if entry.duration:
                message += f" - {duration_to_str(entry.duration)}\n"
            else:
                message += "\n"
            select.add_option(
                label=trim_option_text(option_label(index, entry)),
                description=(
                    duration_to_str(entry.duration) if entry.duration else None
                ),
            )

        async def on_reject(button_interaction: discord.Interaction):
            if interaction.user.id != button_interaction.user.id:
                return await button_interaction.response.send_message(
                    "Fuck off.", ephemeral=True, delete_after=5
                )
            await button_interaction.response.defer()
            await interaction.delete_original_response()

        view.add_item(select)
        view.add_item(
            ButtonView(
                callback=on_reject,
                label="Cancel",
                style=discord.ButtonStyle.red,
            )
        )

        await interaction.response.send_message(
            f"```{message}```",
            view=view,
        )

    async def queue(self, interaction: discord.Interaction) -> None:
        print("[ ] queue")
        assert interaction.guild

        tracks = (
            await self.state[interaction.guild.id].enqueued_tracks()
            if interaction.guild.id in self.state
            else []
        )
        message = ""
        for i, track in enumerate(tracks):
            message += f"{i + 1}. {track.title}\n"

        await interaction.response.send_message(
            f"```{message}```" if message else "Empty.", ephemeral=True, delete_after=5
        )

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
        assert interaction.guild_id
        state = self.__guild_state(interaction.guild_id)
        await (await state.voice_client(interaction)).move_to(author.voice.channel)
        await interaction.response.send_message(
            "Joined.", ephemeral=True, delete_after=5
        )

    async def skip(self, interaction: discord.Interaction) -> None:
        print("[ ] skip")
        await self.__ensure_playing(interaction)
        assert interaction.guild
        await self.state[interaction.guild.id].skip()
        await interaction.response.send_message(
            "Skipped.", ephemeral=True, delete_after=5
        )

    @discord.app_commands.describe(
        nightcore_factor=PlaybackOptions.NIGHTCORE_FACTOR_DOC,
        bassboost_factor=PlaybackOptions.BASSBOOST_FACTOR_DOC,
        volume=PlaybackOptions.VOLUME_DOC,
    )
    async def set(
        self,
        interaction: discord.Interaction,
        nightcore_factor: float | None,
        bassboost_factor: float | None,
        volume: int | None,
    ):
        print("[ ] set")
        await self.__ensure_playing(interaction)
        effective_volume = min(max(volume, 0), 200) / 100 if volume else None
        options = PlaybackOptions(
            nightcore_factor=nightcore_factor,
            bassboost_factor=bassboost_factor,
            volume=effective_volume,
        )
        assert interaction.guild
        await self.state[interaction.guild.id].set_options(options)
        await interaction.response.send_message(
            "Applied playback settings.",
            ephemeral=True,
            delete_after=5,
        )

    @discord.app_commands.describe(volume="Number from 0 to 200.")
    async def volume(self, interaction: discord.Interaction, volume: int) -> None:
        await self.__ensure_playing(interaction)
        print(f"[ ] volume {volume}")
        volume = max(min(volume, 200), 0)

        assert interaction.guild
        state = self.__guild_state(interaction.guild.id)
        voice_client = await state.voice_client(interaction)
        state.set_volume(volume / 100)
        cast(YTDLSource, voice_client.source).volume = volume / 100
        await interaction.response.send_message(f"Volume set to {volume}%.")

    @discord.app_commands.describe(factor=PlaybackOptions.NIGHTCORE_FACTOR_DOC)
    async def nightcore(
        self, interaction: discord.Interaction, factor: float | None
    ) -> None:
        await self.set(
            interaction,
            nightcore_factor=factor or 1.1,
            bassboost_factor=None,
            volume=None,
        )

    @discord.app_commands.describe(factor=PlaybackOptions.BASSBOOST_FACTOR_DOC)
    async def bassboost(
        self, interaction: discord.Interaction, factor: float | None
    ) -> None:
        await self.set(
            interaction,
            nightcore_factor=None,
            bassboost_factor=factor or 10.0,
            volume=None,
        )

    async def denightcore(self, interaction: discord.Interaction) -> None:
        await self.set(
            interaction,
            nightcore_factor=1,
            bassboost_factor=None,
            volume=None,
        )

    async def debassboost(self, interaction: discord.Interaction) -> None:
        await self.set(
            interaction,
            nightcore_factor=None,
            bassboost_factor=0.0,
            volume=None,
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
        assert interaction.guild_id
        state = self.__guild_state(interaction.guild_id)
        voice_client = state.current_voice_client()
        if voice_client is not None:
            await voice_client.disconnect(force=False)
        await interaction.response.send_message("Left.", ephemeral=True, delete_after=5)

    async def die(self, interaction: discord.Interaction) -> None:
        print("[ ] die")
        await interaction.response.send_message(
            "About to die.", ephemeral=True, delete_after=5
        )
        sys.exit(0)

    async def ping(self, interaction: discord.Interaction) -> None:
        print("[ ] ping")
        await interaction.response.send_message("Pong.", ephemeral=True, delete_after=5)

    async def __ensure_voice(self, interaction: discord.Interaction) -> None:
        member = cast(discord.Member, interaction.user)
        if member.voice is None:
            await interaction.response.send_message(
                "No voice channel, dumbass.", ephemeral=True, delete_after=5
            )
            raise discord.ext.commands.CommandError("Not connected to a voice channel.")

    async def __ensure_playing(self, interaction: discord.Interaction) -> None:
        await self.__ensure_voice(interaction)
        assert interaction.guild
        if (
            interaction.guild.id not in self.state
            or not self.state[interaction.guild.id].is_playing()
        ):
            await interaction.response.send_message(
                "Not playing, dumbass.", ephemeral=True, delete_after=5
            )
            raise discord.ext.commands.CommandError("Audio not playing.")

    def __guild_state(self, guild_id: int) -> GuildState:
        if guild_id in self.state:
            return self.state[guild_id]
        state = GuildState(guild_id, self.executor, self.bot)
        self.state[guild_id] = state
        return state
