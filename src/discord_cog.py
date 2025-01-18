import asyncio
import dataclasses
from typing import cast, override, Callable, Awaitable, Dict, Any, Tuple
import io
import sys
from concurrent.futures import Executor
import html
import aiohttp
import discord
import discord.ext.commands
import validators
from jellyfin_apiclient_python import JellyfinClient
from src.audio import YTDLQueuedStreamAudio, YTDLSource, PlaybackOptions
from src.util import (
    iso8601_to_unix_timestamp,
    jf_best_thumbnail_url,
    trim_option_text,
    yt_best_thumbnail_url,
    add_to_embed,
    duration_to_str,
)


AUDIO_BITRATE = 320


class SelectTrack(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__()
        self._callback: Callable[[discord.Interaction], Awaitable[None]] | None = None

    def selected_value(self) -> str:
        return self.values[0]

    def set_callback(self, callback: Callable[[discord.Interaction], Awaitable[None]]):
        self._callback = callback

    @override
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
        self._queue_lock = asyncio.Lock()

    async def enqueue(
        self,
        voice_client: discord.VoiceClient,
        url: str,
        options: PlaybackOptions,
    ) -> None:
        async with self._queue_lock:
            await self._queue.add(url, options)
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
        async with self._queue_lock:
            if self._source is not None and self._source.buffered_audio:
                self._source.buffered_audio.drain()
            if self._queue:
                await self._queue.skip()

    def is_playing(self) -> bool:
        return self._is_playing

    async def cleanup(self) -> None:
        async with self._queue_lock:
            if self._source is not None:
                self._source.cleanup()
            self._queue.cleanup()
            self._queue.clear()

    def set_volume(self, value: float) -> None:
        self._volume = value


@dataclasses.dataclass
class MessageContent:
    title: str
    url: str | None = None
    artwork_url: str | None = None
    color: discord.Color | None = None
    author_name: str | None = None
    author_url: str | None = None


@dataclasses.dataclass
class SearchEntry:
    name: str
    url: str
    on_select_message: MessageContent
    duration: int | None = None


@dataclasses.dataclass
class JellyfinLibraryClient:
    client: JellyfinClient
    library_id: str


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
        ]:
            bot.tree.add_command(cast(discord.app_commands.Command, command))

    @discord.app_commands.describe(
        query="Either a url or a search query.",
        nightcore_factor=PlaybackOptions.NIGHTCORE_FACTOR_DOC,
        bassboost_factor=PlaybackOptions.BASSBOOST_FACTOR_DOC,
        filter_graph=PlaybackOptions.FILTER_GRAPH_DOC,
    )
    async def yt(
        self,
        interaction: discord.Interaction,
        query: str,
        nightcore_factor: float | None,
        bassboost_factor: float | None,
        filter_graph: str | None,
    ) -> None:
        print("[ ] yt app command")
        await self.__authorize_options(interaction, filter_graph)
        options = PlaybackOptions(
            nightcore_factor=nightcore_factor,
            bassboost_factor=bassboost_factor,
            filter_graph=filter_graph,
        )
        if validators.url(query):
            await self.__enqueue(
                await self.__voice_client(interaction),
                query,
                options=options,
            )
            await interaction.response.send_message(
                content=f"{query}{f"\n{options}" if options else ""}"
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
                title = html.unescape(entry["snippet"]["title"])
                video_url = f"https://youtube.com/watch?v={entry["id"]}"
                author_url = (
                    f"https://youtube.com/channel/{entry["snippet"]["channelId"]}"
                )
                entries.append(
                    SearchEntry(
                        name=title,
                        url=video_url,
                        on_select_message=MessageContent(
                            title=title,
                            url=video_url,
                            artwork_url=yt_best_thumbnail_url(entry),
                            color=discord.Color.red(),
                            author_name=entry["snippet"]["channelTitle"],
                            author_url=author_url,
                        ),
                        duration=iso8601_to_unix_timestamp(
                            entry["contentDetails"]["duration"]
                        ),
                    )
                )
        await self.__search_result_select(interaction, entries, options)

    @discord.app_commands.describe(
        query="Search query.",
        nightcore_factor=PlaybackOptions.NIGHTCORE_FACTOR_DOC,
        bassboost_factor=PlaybackOptions.BASSBOOST_FACTOR_DOC,
        filter_graph=PlaybackOptions.FILTER_GRAPH_DOC,
    )
    async def jf(
        self,
        interaction: discord.Interaction,
        query: str,
        nightcore_factor: float | None,
        bassboost_factor: float | None,
        filter_graph: str | None,
    ) -> None:
        await self.__authorize_options(interaction, filter_graph)

        if self.jellyfin_client is None:
            await interaction.response.send_message(
                "Jellyfin not set up.", ephemeral=True
            )
            return

        print(f"[ ] jf {query}")
        result = await asyncio.to_thread(
            self.jellyfin_client.client.jellyfin.search_media_items,
            media="Audio",
            term=query,
            parent_id=self.jellyfin_client.library_id,
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
                    url=self.jellyfin_client.client.jellyfin.download_url(entry["Id"]),
                    on_select_message=MessageContent(
                        title=entry["Name"],
                        artwork_url=jf_best_thumbnail_url(
                            self.jellyfin_client.client, entry
                        ),
                        author_name=artist_name,
                        color=discord.Color.blue(),
                    ),
                    duration=entry["RunTimeTicks"] // 10_000_000,
                )
            )
        await self.__search_result_select(
            interaction,
            entries,
            PlaybackOptions(
                nightcore_factor=nightcore_factor,
                bassboost_factor=bassboost_factor,
                filter_graph=filter_graph,
            ),
        )

    async def __authorize_options(
        self, interaction: discord.Interaction, filter_graph: str | None
    ):
        if interaction.user.id != self.discord_admin_id and filter_graph:
            await interaction.response.send_message(
                "Not allowed to use the filter_graph option.", ephemeral=True
            )
            raise discord.ext.commands.CommandError(
                "not authorized to use filter_graph option"
            )

    async def __create_embed(
        self, item: SearchEntry, options: PlaybackOptions
    ) -> Tuple[discord.Embed, discord.File]:
        assert item.on_select_message.artwork_url
        async with await self.http.get(item.on_select_message.artwork_url) as image:
            image.raise_for_status()
            embed = discord.Embed(
                title=item.on_select_message.title,
                url=item.on_select_message.url,
                color=item.on_select_message.color,
            )
            embed.set_author(
                name=item.on_select_message.author_name,
                url=item.on_select_message.author_url,
            )
            embed.set_image(url="attachment://artwork.jpg")
            add_to_embed(embed, options)
            return embed, discord.File(
                io.BytesIO(await image.content.read()),
                filename="artwork.jpg",
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

        select = SelectTrack()
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

        view = discord.ui.View()
        view.add_item(select)

        dismissed_lock = asyncio.Lock()
        dismissed = False

        async def edit_original_response(item: SearchEntry) -> None:
            if item.on_select_message.artwork_url:
                embed, attachment = await self.__create_embed(item, options)
                await interaction.edit_original_response(
                    content=None,
                    embed=embed,
                    attachments=[attachment],
                    view=None,
                )
            else:
                message = ""
                if item.on_select_message.author_name:
                    message += f"{item.on_select_message.author_name} - "
                message += item.on_select_message.title
                if options:
                    message += f"\n{options}"
                await interaction.edit_original_response(content=message, view=None)

        async def on_selected(selection_interaction: discord.Interaction) -> None:
            if interaction.user.id != selection_interaction.user.id:
                await selection_interaction.response.send_message(
                    "Fuck off.", ephemeral=True, delete_after=5
                )
                return
            nonlocal dismissed
            async with dismissed_lock:
                if dismissed:
                    return
                try:
                    item = [
                        entry
                        for index, entry in enumerate(entries)
                        if trim_option_text(option_label(index, entry))
                        == select.selected_value()
                    ][0]
                    await self.__enqueue(
                        await self.__voice_client(selection_interaction),
                        item.url,
                        options=options,
                    )
                    await edit_original_response(item)
                    dismissed = True
                except discord.DiscordException as e:
                    print(f"[ ] Interaction error: {e}")
                    await interaction.edit_original_response(view=view)

        select.set_callback(on_selected)
        await interaction.response.send_message(
            f"```{message}```",
            view=view,
        )
        await asyncio.sleep(30)
        async with dismissed_lock:
            if not dismissed:
                await interaction.delete_original_response()
                dismissed = True

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

    async def __enqueue(
        self,
        voice_client: discord.VoiceClient,
        url: str,
        options: PlaybackOptions,
    ) -> None:
        state = self.__guild_state(voice_client.guild.id)
        await state.enqueue(voice_client, url, options)
