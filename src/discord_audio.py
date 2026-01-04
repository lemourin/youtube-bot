import asyncio
from concurrent.futures import Executor
from discord.opus import Encoder as OpusEncoder
import discord
from typing import cast, override
from .util import PlaybackOptions
from .discord_util import add_to_embed
from .audio_util import YTDLQueuedStreamAudio, BufferedAudioSource, AudioTrack

AUDIO_PACKET_SIZE = 3840


def update_playback_options(queue: YTDLQueuedStreamAudio, options: PlaybackOptions):
    async def update_embed(interaction: discord.Interaction, options: PlaybackOptions):
        message = await interaction.original_response()
        embed = message.embeds[0]
        add_to_embed(embed, options)
        await interaction.edit_original_response(embeds=message.embeds)

    def on_update(track: AudioTrack):
        interaction = cast(discord.Interaction, track.user_data)
        asyncio.ensure_future(update_embed(interaction, options), loop=queue.main_loop)

    queue.set_options(options, on_update)


class AudioSource(discord.AudioSource):
    def __init__(self, source: BufferedAudioSource):
        self.source = source

    @override
    def read(self) -> bytes:
        return self.source.read()

    @override
    def cleanup(self) -> None:
        return self.source.cleanup()


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(
        self, queue: YTDLQueuedStreamAudio, volume: float, executor: Executor
    ) -> None:
        self.source = BufferedAudioSource(
            queue, executor, chunk_size=OpusEncoder.FRAME_SIZE
        )
        super().__init__(AudioSource(self.source), volume)

    def drain(self) -> None:
        self.source.drain()

    def is_done(self) -> bool:
        return self.source.is_done()
