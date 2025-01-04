import discord
import discord.ext.commands
import asyncio
import time
import subprocess
import threading
import os
from dotenv import load_dotenv
import sys
import aiohttp

load_dotenv()

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_ADMIN_ID = int(os.environ["DISCORD_ADMIN_ID"])
HEALTHCHECK_ADDRESS = os.environ["HEALTHCHECK_ADDRESS"]
# AUDIO_BITRATE = 128
AUDIO_BITRATE = 512


class YTDLBuffer:
    def __init__(self, url):
        self.initialized = False
        self.url = url
        self.proc = None

    def ensure_initialized(self):
        if self.initialized:
            return
        print(f"[ ] YTDLBuffer creating process for {self.url}")
        args = [
            "-x",
            # Audio options, breaks generic downloader.
            # "-f",
            # "bestaudio",
            "--cookies",
            "cookie.txt",
            "--no-warnings",
            self.url,
            "-o",
            "-",
            "--quiet",
        ]
        print("[ ] yt-dlp", *args)
        self.proc = subprocess.Popen(
            executable="yt-dlp",
            args=args,
            stdout=asyncio.subprocess.PIPE,
            bufsize=0,
        )
        self.initialized = True

    def read(self, n):
        # print("[ ] YTDLBuffer read")
        self.ensure_initialized()
        return self.proc.stdout.read(n)

    def cleanup(self):
        print("[ ] YTDLBuffer cleanup")
        if not self.initialized:
            return
        if self.proc is not None:
            self.proc.terminate()
            self.proc = None
            print("[ ] process cleaned up")


class YTDLStreamAudio(discord.FFmpegPCMAudio):
    def __init__(self, url):
        self.buffer = YTDLBuffer(url)
        super().__init__(self.buffer, pipe=True, options="-vn")

    def cleanup(self):
        print("[ ] YTDLStreamAudio cleanup")
        self.buffer.cleanup()
        super().cleanup()


class YTDLQueuedStreamAudio(discord.AudioSource):
    def __init__(self):
        super().__init__()
        self.queue = []
        self.read_size = 3840
        self.zeros = b"\0" * self.read_size

    def add(self, url):
        print(f"[ ] adding {url} to queue")
        self.queue.append(url)
        if len(self.queue) == 2:
            self.queue[1] = YTDLStreamAudio(self.queue[1])

    def clear(self):
        print("[ ] clearing queue")
        trash = self.queue
        self.queue = []
        for a in trash:
            a.cleanup()

    def skip(self):
        if not self.queue:
            return
        a = self.queue[0]
        self.queue = self.queue[1:]

        if len(self.queue) > 1:
            self.queue[1] = YTDLStreamAudio(self.queue[1])
        a.cleanup()

    def read(self):
        # print("[ ] YTDLQueuedStreamAudio read")
        trash = None
        if not self.queue:
            print("[ ] queue empty")
            return b""
        if type(self.queue[0]) is str:
            self.queue[0] = YTDLStreamAudio(self.queue[0])
        c = self.queue[0].read()
        # print(f"[ ] YTDLQueuedStreamAudio got {len(c)} bytes from queue head")
        if len(c) < self.read_size:
            if len(self.queue) > 1:
                c = c + self.zeros[len(c) :]
            trash = self.queue[0]
            print("[ ] advancing queue")
            self.queue = self.queue[1:]
            if len(self.queue) > 1:
                self.queue[1] = YTDLStreamAudio(self.queue[1])
        if trash is not None:
            trash.cleanup()
        return c

    def is_opus(self):
        return False

    def cleanup(self):
        print("[ ] YTDLQueuedStreamAudio cleanup")
        trash = self.queue
        self.queue = []
        for a in trash:
            a.cleanup()


class EagerPCMAudio(discord.AudioSource):
    def __init__(self, source):
        self.initialized = False
        self.max_chunk_count = 128
        self.preload_chunk_count = 128
        self.backoff_sec = 1.0
        self.source = source
        self.chunks = []
        self.access_sem = threading.Semaphore()
        self.chunk_sem = threading.Semaphore(value=0)
        self.fetcher_thread = None
        self.close_event = threading.Event()

    def drain(self):
        pass
        # wojcu@ nie ogarnia jak wyczyscic bufor.
        # with self.access_sem:
        #     self.chunks.clear()

    def ensure_initialized(self):
        if self.initialized:
            return

        for _ in range(self.preload_chunk_count):
            data = self.source.read()
            if len(data) == 0:
                self.source = None
                self.chunk_sem.release()
                print("[ ] EagerPCMAudio source done")
                break
            with self.access_sem:
                self.chunks.append(data)
            self.chunk_sem.release()

        self.fetcher_thread = threading.Thread(target=self.fetcher_main)
        self.fetcher_thread.start()

        self.initialized = True

    def read(self):
        # print("[ ] EagerPCMAudio read")
        self.ensure_initialized()
        self.chunk_sem.acquire()
        with self.access_sem:
            if not self.chunks:
                print("[ ] EagerPCMAudio finished")
            else:
                c = self.chunks[0]
                self.chunks = self.chunks[1:]
                return c
        self.chunk_sem.release()
        return b""

    def fetcher_main(self):
        while True:
            if self.close_event.is_set():
                print("[ ] EagerPCMAudio fetecher stopped, close event")
                break
            with self.access_sem:
                l = len(self.chunks)
            if l >= self.max_chunk_count:
                time.sleep(self.backoff_sec)
                with self.access_sem:
                    l = len(self.chunks)
                if l < 8:
                    print(f"[!] fetcher has only {l} chunks ready")
                continue

            if self.source is None:
                print("[ ] EagerPCMAudio fetecher stopped, source empty")
                break
            data = self.source.read()
            if len(data) == 0:
                self.source = None
                self.chunk_sem.release()
                print("[ ] EagerPCMAudio fetecher stopped, finished")
                break
            with self.access_sem:
                self.chunks.append(data)
            self.chunk_sem.release()

    def cleanup(self):
        print("[ ] EagerPCMAudio cleanup")
        if not self.initialized:
            return
        self.close_event.set()
        self.fetcher_thread.join()


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, queue, volume):
        self.queue = queue
        self.eager_audio = EagerPCMAudio(queue)
        super().__init__(self.eager_audio, volume)

    def prefetch(self):
        self.eager_audio.ensure_initialized()


class Audio(discord.ext.commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queue = None
        self.source = None

        self.is_playing = False

        self.volume = 0.1

    @discord.ext.commands.command()
    async def join(self, ctx):
        print("[ ] join")
        if ctx.voice_client is not None:
            return await ctx.voice_client.move_to(ctx.author.voice.channel)

        await ctx.author.voice.channel.connect()

    @discord.ext.commands.command()
    async def yt(self, ctx, *, url):
        print(f"[ ] yt {url}")
        if self.queue is None:
            self.queue = YTDLQueuedStreamAudio()
        self.queue.add(url)
        if not self.is_playing:
            print("[ ] voice client not playing, starting")
            self.is_playing = True
            async with ctx.typing():
                self.source = YTDLSource(self.queue, self.volume)
                self.source.prefetch()

            def finalizer(self, err):
                if err:
                    print(f"[!] player error: {err}")
                else:
                    print(f"[ ] finished playing")
                    self.is_playing = False

            ctx.voice_client.play(
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
    async def skip(self, ctx):
        print(f"[ ] skip")
        self.source.eager_audio.drain()
        if self.queue is not None:
            self.queue.skip()

    @discord.ext.commands.command()
    async def volume(self, ctx, volume: int):
        print(f"[ ] volume {volume}")
        if volume < 0:
            volume = 0
        if volume > 200:
            volume = 200

        self.volume = volume / 100
        ctx.voice_client.source.volume = self.volume
        await ctx.send(f"volume set to {volume}%")

    @discord.ext.commands.command()
    async def stop(self, ctx):
        print("[ ] stop")
        if ctx.voice_client.source is not None:
            ctx.voice_client.stop()
        if self.source is not None:
            self.source.cleanup()
            self.source = None
        if self.queue is not None:
            self.queue.cleanup()
            self.queue.clear()

    @discord.ext.commands.command()
    async def leave(self, ctx):
        print("[ ] leave")
        if ctx.voice_client is not None:
            await ctx.voice_client.disconnect()
        if self.source is not None:
            self.source.cleanup()
            self.source = None
        if self.queue is not None:
            self.queue.clear()
            self.queue.cleanup()
            self.queue = None

    @discord.ext.commands.command()
    async def die(self, ctx):
        print("[ ] die")
        sys.exit(0)

    @discord.ext.commands.command()
    async def ping(self, ctx):
        print("[ ] ping")
        await ctx.send(f"pong")

    @yt.before_invoke
    @join.before_invoke
    async def ensure_voice(self, ctx):
        if ctx.voice_client is None:
            if ctx.author.voice:
                await ctx.author.voice.channel.connect()
            else:
                await ctx.send("no voice channel, dumbass")
                raise discord.ext.commands.CommandError(
                    "author not connected to a voice channel."
                )

    @volume.before_invoke
    @stop.before_invoke
    @skip.before_invoke
    async def ensure_playing(self, ctx):
        if ctx.voice_client is None:
            await ctx.send("no voice channel, dumbass")
            raise discord.ext.commands.CommandError("not connected to a voice channel")

        if not self.is_playing:
            await ctx.send("not playing, dumbass")
            raise discord.ext.commands.CommandError("audio not playing")

    # @volume.before_invoke
    async def ensure_auth(self, ctx):
        author_id = None if ctx.author is None else ctx.author.id
        if author_id != DISCORD_ADMIN_ID:
            await ctx.send("fuck off, pleb")
            raise discord.ext.commands.CommandError("not authorirized")


intents = discord.Intents.default()
intents.message_content = True

bot = discord.ext.commands.Bot(
    command_prefix=discord.ext.commands.when_mentioned_or("!"),
    intents=intents,
)


@bot.event
async def on_ready():
    print(f"[ ] logged in {bot.user} {bot.user.id}")


@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel is None:
        return
    if len(before.channel.members) != 1:
        return
    bot_member = before.channel.members[0]
    if bot_member.id != bot.user.id:
        return
    await bot_member.guild.voice_client.disconnect(force=True)


async def healthcheck():
    if len(HEALTHCHECK_ADDRESS) == 0:
        return
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(HEALTHCHECK_ADDRESS) as response:
                    await response.text()
            except e:
                print(f"[ ] health check error {e}")
            await asyncio.sleep(60)


async def main():
    async with bot:
        await bot.add_cog(Audio(bot))
        await asyncio.gather(bot.start(DISCORD_BOT_TOKEN), healthcheck())


if __name__ == "__main__":
    asyncio.run(main())
