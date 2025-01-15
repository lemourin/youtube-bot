import subprocess
import threading
import asyncio
from typing import cast
import io
from concurrent.futures import Executor
import json
import discord


class YTDLBuffer(io.BufferedIOBase):
    def __init__(self, url: str) -> None:
        self.proc = YTDLBuffer.create_process(url)

    def read(self, n: int | None = None) -> bytes:
        # print("[ ] YTDLBuffer read")
        assert self.proc.stdout is not None
        return self.proc.stdout.read(-1 if n is None else n)

    def cleanup(self) -> None:
        print("[ ] YTDLBuffer cleanup")
        self.proc.terminate()
        print("[ ] process cleaned up")

    @staticmethod
    def create_process(url: str) -> subprocess.Popen[bytes]:
        print(f"[ ] YTDLBuffer creating process for {url}")
        args = [
            "-x",
            # Audio options, breaks generic downloader.
            # "-f",
            # "bestaudio",
            "--cookies",
            "cookie.txt",
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


class PlaybackOptions:
    NIGHTCORE_FACTOR_DOC = "Factor of how much to speed up the audio. [0.5, 1.5]."
    BASSBOOST_FACTOR_DOC = "Factor of how much to bassboost the audio. [-10, 10]."
    FILTER_GRAPH_DOC = "Custom ffmpeg audio filtering graph."

    def __init__(
        self,
        nightcore_factor: float | None = None,
        bassboost_factor: float | None = None,
        filter_graph: str | None = None,
    ) -> None:
        self.nightcore_factor = nightcore_factor
        self.bassboost_factor = bassboost_factor
        self.filter_graph = filter_graph

    def __bool__(self) -> bool:
        return (
            self.nightcore_factor is not None
            or self.bassboost_factor is not None
            or self.filter_graph is not None
        )

    def __str__(self) -> str:
        message = ""
        if self.nightcore_factor:
            message += f"* nightcore_factor = {self.nightcore_factor}\n"
        if self.bassboost_factor:
            message += f"* bassboost_factor = {self.bassboost_factor}\n"
        if self.filter_graph:
            message += f"* filter_graph = {self.filter_graph}"
        return message


class YTDLStreamAudio(discord.FFmpegPCMAudio):
    def __init__(self, url: str, options: PlaybackOptions) -> None:
        self.buffer = YTDLBuffer(url)

        ffmpeg_options = ""

        def append(opt: str):
            nonlocal ffmpeg_options
            if not ffmpeg_options:
                ffmpeg_options = "-af "
            else:
                ffmpeg_options += ","
            ffmpeg_options += opt

        if options.nightcore_factor:
            probe = self.__probe(url)
            sample_rate = 44100
            for stream in probe["streams"]:
                if stream["codec_type"] == "audio":
                    sample_rate = int(stream["sample_rate"])
                    break
            append(f"asetrate={sample_rate * options.nightcore_factor}")
        if options.bassboost_factor:
            append(f"bass=g={options.bassboost_factor}")
        if options.filter_graph:
            append(options.filter_graph)
        super().__init__(
            self.buffer, pipe=True, options=f"-vn -loglevel error {ffmpeg_options}"
        )

    def __probe(self, url: str) -> dict:
        with YTDLBuffer.create_process(url) as input_process, subprocess.Popen(
            executable="ffprobe",
            args=[
                "-i",
                "-",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-hide_banner",
                "-loglevel",
                "error",
            ],
            stdin=input_process.stdout,
            stdout=asyncio.subprocess.PIPE,
        ) as process:
            assert process.stdout
            return json.loads(process.stdout.read())

    def cleanup(self) -> None:
        print("[ ] YTDLStreamAudio cleanup")
        self.buffer.cleanup()
        super().cleanup()


class LazyAudioSource(discord.AudioSource):
    def __init__(self, url: str, options: PlaybackOptions) -> None:
        self.url = url
        self.source: discord.AudioSource | None = None
        self.options = options

    def prefetch(self) -> None:
        if self.source is None:
            self.source = YTDLStreamAudio(self.url, self.options)

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

    async def add(self, url: str, options: PlaybackOptions) -> None:
        print(f"[ ] adding {url} to queue")
        self.queue.append(LazyAudioSource(url, options))
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
