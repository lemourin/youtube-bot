import asyncio
from concurrent.futures import Executor
from typing import cast
import time
import discord
import discord.ext.commands
from src.audio import YTDLQueuedStreamAudio, YTDLSource, AudioTrack, PlaybackOptions
from src.discord_ui import View, ButtonView

AUDIO_BITRATE = 320


class GuildState:
    def __init__(
        self,
        guild_id: int,
        executor: Executor,
        bot: discord.ext.commands.Bot,
    ) -> None:
        self._guild_id = guild_id
        self._executor = executor
        self._bot = bot
        self._queue = YTDLQueuedStreamAudio(
            executor, self._on_enqueued, self._on_dequeued
        )
        self._source: YTDLSource | None = None
        self._is_playing = False
        self._volume = 0.1
        self._queue_lock = asyncio.Lock()
        self._cb_lock = asyncio.Lock()

    async def _on_enqueued(self, track: AudioTrack) -> None:
        async with track.lock:
            print(f"[ ] enqueued {track.title}")
            if track.on_enqueue:
                await track.on_enqueue()
            if track.can_edit_message:
                await track.interaction.edit_original_response(
                    view=self.new_playback_control_view(track.interaction, track)
                )
            track.on_enqueue_time = int(time.time() * 1000)

    async def _on_dequeued(self, track: AudioTrack) -> None:
        min_enqueue_time = 3000
        async with track.lock:
            if track.on_enqueue_time:
                current_time = int(time.time() * 1000)
                duration = current_time - track.on_enqueue_time
                if duration < min_enqueue_time:
                    await asyncio.sleep((min_enqueue_time - duration) // 1000)
            print(f"[ ] dequeued {track.title}")
            if track.on_dequeue:
                await track.on_dequeue()
            if track.can_edit_message:
                await track.interaction.edit_original_response(
                    view=self.new_playback_control_view(track.interaction, track)
                )

    async def enqueue(
        self,
        voice_client: discord.VoiceClient,
        track: AudioTrack,
    ) -> None:
        async with self._queue_lock:
            await self._enqueue(voice_client, track)

    async def _enqueue(
        self,
        voice_client: discord.VoiceClient,
        track: AudioTrack,
    ) -> None:
        await self._queue.add(track)
        await self._start(voice_client)

    async def _start(self, voice_client: discord.VoiceClient) -> None:
        if self._source and not self._source.buffered_audio.is_done():
            return

        print("[ ] voice client not playing, starting")
        self._is_playing = True
        if self._source:
            source = self._source
            self._source = None
            await asyncio.to_thread(source.cleanup)
        self._source = await asyncio.to_thread(
            lambda: YTDLSource(
                self._queue,
                self._volume,
                self._executor,
            )
        )

        def finalizer(self: GuildState, err: Exception | None):
            if err:
                print(f"[!] player error: {err}")
            else:
                print("[ ] finished playing")
                self._is_playing = False

        if self._is_playing:
            voice_client.pause()
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

    async def skip(self, track_id: int | None = None) -> None:
        async with self._queue_lock:
            await self._skip(track_id)

    async def is_skippable(self, track_id: int) -> bool:
        async with self._queue_lock:
            return self._queue.current_position(track_id) is not None

    async def _skip(self, track_id: int | None = None) -> None:
        if self._source is not None and (
            track_id is None or self._queue.current_track_id() == track_id
        ):
            self._source.buffered_audio.drain()
        await self._queue.skip(track_id)

    async def play_now(
        self,
        voice_client: discord.VoiceClient,
        track: AudioTrack,
    ) -> None:
        async with self._queue_lock:
            if self._source is not None:
                self._source.buffered_audio.drain()
            await self._queue.play_now(track)
            await self._start(voice_client)

    async def current_track_id(self) -> int | None:
        async with self._queue_lock:
            return self._queue.current_track_id()

    def is_playing(self) -> bool:
        return self._is_playing

    async def cleanup(self) -> None:
        async with self._queue_lock:
            if self._source is not None:
                self._source.cleanup()
            self._queue.cleanup()

    def set_volume(self, value: float) -> None:
        self._volume = value

    async def set_options(self, options: PlaybackOptions) -> None:
        async with self._queue_lock:
            await asyncio.get_event_loop().run_in_executor(
                self._executor, self._queue.set_options, options
            )

    async def enqueued_tracks(self) -> list[AudioTrack]:
        async with self._queue_lock:
            return [e.track for e in self._queue.queue]

    def current_voice_client(self) -> discord.VoiceClient | None:
        for e in self._bot.voice_clients:
            voice_client = cast(discord.VoiceClient, e)
            if voice_client.guild.id == self._guild_id:
                return voice_client
        return None

    async def voice_client(
        self, interaction: discord.Interaction
    ) -> discord.VoiceClient:
        voice_client = self.current_voice_client()
        if voice_client:
            return voice_client
        assert isinstance(interaction.user, discord.Member)
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.response.send_message(
                "no voice channel, dumbass", ephemeral=True
            )
            raise discord.ext.commands.CommandError("not connected to a voice channel")
        return await interaction.user.voice.channel.connect()

    def __create_playback_control_view(
        self,
        interaction: discord.Interaction,
        track: AudioTrack,
        enable_skip: bool = True,
        enable_play_now: bool = True,
    ) -> discord.ui.View:
        async def on_timeout():
            await interaction.edit_original_response(view=None)

        def create_view(
            enable_skip: bool = True, enable_play_now: bool = True
        ) -> discord.ui.View:
            view = View(on_timeout=on_timeout, timeout=300)
            view.add_item(
                ButtonView(
                    style=discord.ButtonStyle.red,
                    label="Skip",
                    callback=on_skip,
                    disabled=not enable_skip,
                )
            )
            view.add_item(
                ButtonView(
                    style=discord.ButtonStyle.blurple,
                    label="Play Now",
                    callback=on_play_now,
                    disabled=not enable_play_now,
                )
            )
            return view

        async def on_skip(interaction: discord.Interaction):
            if not await self.is_skippable(track.track_id):
                return await interaction.response.send_message(
                    "Track not enqueued.", ephemeral=True, delete_after=5
                )
            await self.skip(track.track_id)
            await interaction.response.edit_message(view=create_view(enable_skip=False))

        async def on_play_now(interaction: discord.Interaction):
            if await self.current_track_id() == track.track_id:
                return await interaction.response.send_message(
                    "Already playing.", ephemeral=True, delete_after=5
                )
            await self.play_now(await self.voice_client(interaction), track)
            await interaction.response.edit_message(
                view=create_view(enable_play_now=False)
            )

        return create_view(enable_skip, enable_play_now)

    def new_playback_control_view(
        self,
        interaction: discord.Interaction,
        track: AudioTrack,
    ) -> discord.ui.View:
        return self.__create_playback_control_view(
            interaction,
            track,
            enable_skip=self._queue.current_position(track.track_id) is not None,
            enable_play_now=self._queue.current_track_id() != track.track_id,
        )
