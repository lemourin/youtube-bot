import subprocess
import threading
import asyncio
import dataclasses
from typing import cast, override
import io
from concurrent.futures import Executor
import json
import discord


@dataclasses.dataclass
class PlaybackOptions:
    NIGHTCORE_FACTOR_DOC = "Factor of how much to speed up the audio. [0.5, 1.5]."
    BASSBOOST_FACTOR_DOC = "Factor of how much to bassboost the audio. [-10, 10]."
    FILTER_GRAPH_DOC = "Custom ffmpeg audio filtering graph."
    START_TIMESTAMP = "Start timestamp of audio playback. e.g. 4:20"
    STOP_TIMESTAMP = "Stop timestamp of audio playback. e.g. 4:20"

    nightcore_factor: float | None = None
    bassboost_factor: float | None = None
    filter_graph: str | None = None
    start_timestamp: str | None = None
    stop_timestamp: str | None = None


class YTDLBuffer(io.BufferedIOBase):
    def __init__(self, url: str, options: PlaybackOptions) -> None:
        self.proc = YTDLBuffer.create_process(url, options)

    @override
    def read(self, n: int | None = None) -> bytes:
        # print("[ ] YTDLBuffer read")
        assert self.proc.stdout is not None
        return self.proc.stdout.read(-1 if n is None else n)

    def cleanup(self) -> None:
        print("[ ] YTDLBuffer cleanup")
        self.proc.terminate()
        print("[ ] process cleaned up")

    @staticmethod
    def create_process(url: str, options: PlaybackOptions) -> subprocess.Popen[bytes]:
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
            "--download-sections",
            "*from-url",
        ]
        if options.start_timestamp is not None or options.stop_timestamp is not None:
            range_opt = ""
            range_opt += (
                options.start_timestamp
                if options.start_timestamp is not None
                else "0:00"
            )
            range_opt += "-"
            range_opt += (
                options.stop_timestamp if options.stop_timestamp is not None else "inf"
            )
            args.append("--download-sections")
            args.append(f"*{range_opt}")
        print("[ ] yt-dlp", *args)
        return subprocess.Popen(
            executable="yt-dlp",
            args=args,
            stdout=asyncio.subprocess.PIPE,
            bufsize=0,
        )


@dataclasses.dataclass
class AudioTrack:
    url: str
    title: str
    playback_id: int
    playback_options: PlaybackOptions


class YTDLStreamAudio(discord.FFmpegPCMAudio):
    def __init__(self, url: str, options: PlaybackOptions) -> None:
        self.buffer = YTDLBuffer(url, options)

        ffmpeg_options = ""

        def append(opt: str):
            nonlocal ffmpeg_options
            if not ffmpeg_options:
                ffmpeg_options = "-af "
            else:
                ffmpeg_options += ","
            ffmpeg_options += opt

        if options.nightcore_factor:
            probe = self.__probe(url, options)
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

    def __probe(self, url: str, options: PlaybackOptions) -> dict:
        with (
            YTDLBuffer.create_process(url, options) as input_process,
            subprocess.Popen(
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
            ) as process,
        ):
            assert process.stdout
            return json.loads(process.stdout.read())

    @override
    def cleanup(self) -> None:
        print("[ ] YTDLStreamAudio cleanup")
        self.buffer.cleanup()
        super().cleanup()


class LazyAudioSource(discord.AudioSource):
    def __init__(self, track: AudioTrack) -> None:
        self.track = track
        self.source: discord.AudioSource | None = None

    def prefetch(self) -> None:
        if self.source is None:
            self.source = YTDLStreamAudio(self.track.url, self.track.playback_options)

    @override
    def cleanup(self) -> None:
        if self.source:
            self.source.cleanup()
            self.source = None

    @override
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

    async def add(self, track: AudioTrack) -> None:
        print(f"[ ] adding {track.url} to queue")
        self.queue.append(LazyAudioSource(track))
        if len(self.queue) == 2:
            e = self.queue[1]
            await asyncio.to_thread(e.prefetch)

    async def prepend(self, track: AudioTrack) -> None:
        print(f"[ ] prepending {track.url} to queue")
        e = LazyAudioSource(track)
        self.queue.insert(0, e)
        await asyncio.to_thread(e.prefetch)
        if len(self.queue) == 3:
            e = self.queue[2]
            await asyncio.to_thread(e.cleanup)

    async def move(self, playback_id: int, target_position: int) -> None:
        d = self.current_position(playback_id)
        if d is None or d == target_position:
            return

        a = self.queue[d]
        self.queue.pop(d)
        self.queue.insert(target_position, a)
        await asyncio.to_thread(a.prefetch)

        if len(self.queue) > 1:
            e = self.queue[1]
            await asyncio.to_thread(e.prefetch)

    def clear(self) -> None:
        print("[ ] clearing queue")
        trash = self.queue
        self.queue = []
        for a in trash:
            a.cleanup()

    def current_playback_id(self) -> int | None:
        if not self.queue:
            return None
        return self.queue[0].track.playback_id

    def current_position(self, playback_id: int) -> int | None:
        for i, e in enumerate(self.queue):
            if e.track.playback_id == playback_id:
                return i
        return None

    async def skip(self, playback_id: int | None = None) -> None:
        if not self.queue:
            return

        d = None
        if playback_id is None:
            d = 0
        else:
            d = self.current_position(playback_id)

        if d is None:
            return

        a = self.queue[d]
        self.queue.pop(d)
        await asyncio.to_thread(a.cleanup)

        if len(self.queue) > 1:
            e = self.queue[1]
            await asyncio.to_thread(e.prefetch)

    @override
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

    @override
    def is_opus(self) -> bool:
        return False

    @override
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

    @override
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

    @override
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
