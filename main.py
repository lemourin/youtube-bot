#!/usr/bin/env python
# flake8: noqa: E501
# pylint: disable=locally-disabled, missing-class-docstring, missing-module-docstring, missing-function-docstring

import asyncio
import subprocess
import threading
from typing import cast
import os
import io
import sys
from concurrent.futures import Executor, ThreadPoolExecutor
import aiohttp
import discord
import discord.ext.commands
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.environ["ENV_FILE"] if "ENV_FILE" in os.environ else None)

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_BOT_COMMAND_PREFIX = os.environ.get("DISCORD_BOT_COMMAND_PREFIX", "!")
DISCORD_ADMIN_ID = int(os.environ["DISCORD_ADMIN_ID"])
HEALTHCHECK_ADDRESS = os.environ["HEALTHCHECK_ADDRESS"]
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
            "--quiet",
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


class Audio(discord.ext.commands.Cog):
    def __init__(self, bot: discord.ext.commands.Bot, executor: Executor) -> None:
        self.bot = bot
        self.executor = executor
        self.queue: YTDLQueuedStreamAudio | None = None
        self.source: YTDLSource | None = None
        self.is_playing = False
        self._volume = 0.1

    @discord.ext.commands.command()
    async def join(self, ctx: discord.ext.commands.Context) -> None:
        print("[ ] join")
        author = cast(discord.Member, ctx.author)
        assert author.voice is not None
        await cast(discord.VoiceClient, ctx.voice_client).move_to(author.voice.channel)

    @discord.ext.commands.command()
    async def yt(self, ctx: discord.ext.commands.Context, *, url: str) -> None:
        print(f"[ ] yt {url}")
        if self.queue is None:
            self.queue = YTDLQueuedStreamAudio()
        queue = self.queue

        await queue.add(url)
        if self.is_playing:
            return
        print("[ ] voice client not playing, starting")
        self.is_playing = True
        async with ctx.typing():
            self.source = await asyncio.to_thread(
                lambda: YTDLSource(
                    queue,
                    self._volume,
                    self.executor,
                )
            )

        def finalizer(self, err):
            if err:
                print(f"[!] player error: {err}")
            else:
                print("[ ] finished playing")
                self.is_playing = False

        cast(discord.VoiceClient, ctx.voice_client).play(
            self.source,
            signal_type="music",
            bitrate=AUDIO_BITRATE,
            fec=False,
            # fec=True,
            # expected_packet_loss=0.05,
            after=lambda err: finalizer(self, err),
        )

        print("[ ] play started")

    @discord.ext.commands.command()
    async def skip(self, _ctx: discord.ext.commands.Context) -> None:
        print("[ ] skip")
        if self.source and self.source.buffered_audio:
            self.source.buffered_audio.drain()
        queue = self.queue
        if queue is not None:
            await queue.skip()

    @discord.ext.commands.command()
    async def volume(self, ctx: discord.ext.commands.Context, volume: int) -> None:
        print(f"[ ] volume {volume}")
        volume = max(min(volume, 200), 0)
        voice_client = cast(discord.VoiceClient, ctx.voice_client)

        self._volume = volume / 100
        cast(YTDLSource, voice_client.source).volume = self._volume
        await ctx.send(f"volume set to {volume}%")

    @discord.ext.commands.command()
    async def stop(self, ctx: discord.ext.commands.Context) -> None:
        print("[ ] stop")
        voice_client = cast(discord.VoiceClient, ctx.voice_client)
        if voice_client.source is not None:
            voice_client.stop()
        if self.source is not None:
            self.source.cleanup()
            self.source = None
        if self.queue is not None:
            self.queue.cleanup()
            self.queue.clear()

    @discord.ext.commands.command()
    async def leave(self, ctx: discord.ext.commands.Context) -> None:
        print("[ ] leave")
        if ctx.voice_client is not None:
            await ctx.voice_client.disconnect(force=False)
        if self.source is not None:
            self.source.cleanup()
            self.source = None
        if self.queue is not None:
            self.queue.clear()
            self.queue.cleanup()
            self.queue = None

    @discord.ext.commands.command()
    async def die(self, _ctx: discord.ext.commands.Context) -> None:
        print("[ ] die")
        sys.exit(0)

    @discord.ext.commands.command()
    async def ping(self, ctx: discord.ext.commands.Context) -> None:
        print("[ ] ping")
        await ctx.send("pong")

    @yt.before_invoke
    @join.before_invoke
    async def ensure_voice(self, ctx: discord.ext.commands.Context) -> None:
        author = cast(discord.Member, ctx.author)
        if ctx.voice_client is None:
            if author.voice and author.voice.channel:
                await author.voice.channel.connect()
            else:
                await ctx.send("no voice channel, dumbass")
                raise discord.ext.commands.CommandError(
                    "author not connected to a voice channel."
                )

    @volume.before_invoke
    @stop.before_invoke
    @skip.before_invoke
    async def ensure_playing(self, ctx: discord.ext.commands.Context) -> None:
        if ctx.voice_client is None:
            await ctx.send("no voice channel, dumbass")
            raise discord.ext.commands.CommandError("not connected to a voice channel")

        if not self.is_playing:
            await ctx.send("not playing, dumbass")
            raise discord.ext.commands.CommandError("audio not playing")

    # @volume.before_invoke
    async def ensure_auth(self, ctx: discord.ext.commands.Context) -> None:
        author_id = None if ctx.author is None else ctx.author.id
        if author_id != DISCORD_ADMIN_ID:
            await ctx.send("fuck off, pleb")
            raise discord.ext.commands.CommandError("not authorirized")


async def healthcheck() -> None:
    if len(HEALTHCHECK_ADDRESS) == 0:
        return
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(HEALTHCHECK_ADDRESS) as response:
                    await response.text()
            except aiohttp.web_exceptions.HTTPException as e:
                print(f"[ ] health check error {e}")
            await asyncio.sleep(60)


async def main() -> None:
    intents = discord.Intents.default()
    intents.message_content = True

    bot = discord.ext.commands.Bot(
        command_prefix=discord.ext.commands.when_mentioned_or(
            DISCORD_BOT_COMMAND_PREFIX
        ),
        intents=intents,
    )

    @bot.event
    async def on_ready() -> None:
        assert bot.user is not None
        print(f"[ ] logged in {bot.user} {bot.user.id}")

    @bot.event
    async def on_voice_state_update(
        _member: discord.Member, before: discord.VoiceState, _after: discord.VoiceState
    ) -> None:
        if before.channel is None:
            return
        if len(before.channel.members) != 1:
            return
        assert bot.user is not None
        bot_member = before.channel.members[0]
        if bot_member.id != bot.user.id:
            return
        if bot_member.guild.voice_client is not None:
            await bot_member.guild.voice_client.disconnect(force=True)

    with ThreadPoolExecutor(max_workers=32) as executor:
        async with bot:
            await bot.add_cog(Audio(bot, executor))
            await asyncio.gather(bot.start(DISCORD_BOT_TOKEN), healthcheck())


if __name__ == "__main__":
    asyncio.run(main())
