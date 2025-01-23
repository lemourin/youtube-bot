import subprocess
import threading
import asyncio
import dataclasses
from typing import cast, override, Callable
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
    track_id: int
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
        self.lock = threading.Lock()
        self.playback_id: int | None = None

    def prefetch(self) -> None:
        with self.lock:
            self._prefetch()

    def _prefetch(self) -> None:
        if self.source is None:
            self.source = YTDLStreamAudio(self.track.url, self.track.playback_options)

    @override
    def cleanup(self) -> None:
        with self.lock:
            if self.source:
                self.source.cleanup()
                self.source = None
                self.playback_id = None

    @override
    def read(self) -> bytes:
        with self.lock:
            self._prefetch()
            assert self.source
            return self.source.read()


async def _invoke(callbacks: list[Callable[[], None]]):
    def f():
        for i in callbacks:
            i()

    await asyncio.to_thread(f)


@dataclasses.dataclass
class AudioChunk:
    data: bytes
    playback_id: int


class YTDLQueuedStreamAudio:
    def __init__(self) -> None:
        super().__init__()
        self.queue: list[LazyAudioSource] = []
        self.lock = threading.Lock()
        self.read_size = 3840
        self.zeros = b"\0" * self.read_size
        self.current_playback_id = 0

    async def add(self, track: AudioTrack) -> None:
        callbacks = []
        with self.lock:
            print(f"[ ] adding {track.url} to queue")
            self.queue.append(LazyAudioSource(track))
            callbacks += self.__prefetch()
        await _invoke(callbacks)

    async def prepend(self, track: AudioTrack) -> None:
        callbacks = []
        with self.lock:
            print(f"[ ] prepending {track.url} to queue")
            e = LazyAudioSource(track)
            self.queue.insert(0, e)
            callbacks += self.__prefetch()

        await _invoke(callbacks)

    async def skip(self, track_id: int | None = None) -> None:
        callbacks = []
        with self.lock:
            if not self.queue:
                return

            d = None
            if track_id is None:
                d = 0
            else:
                d = self.__current_position(track_id)

            if d is None:
                return

            callbacks.append(self.queue[d].cleanup)
            self.queue.pop(d)

            callbacks += self.__prefetch()

        await _invoke(callbacks)

    async def move(self, track_id: int, target_position: int) -> None:
        callbacks = []
        with self.lock:
            d = self.__current_position(track_id)
            if d is None or d == target_position:
                return

            e = self.queue[d]
            self.queue.pop(d)
            self.queue.insert(target_position, e)
            if target_position >= 2:
                callbacks.append(e.cleanup)

            callbacks += self.__prefetch()

        await _invoke(callbacks)

    async def play_now(self, track: AudioTrack) -> None:
        callbacks = []
        with self.lock:
            position = self.__current_position(track.track_id)
            if position == 0:
                return

            if self.queue:
                callbacks.append(self.queue[0].cleanup)
                self.queue.pop(0)

            if position is None:
                self.queue.insert(0, LazyAudioSource(track))
            elif position > 1:
                position -= 1
                e = self.queue[position]
                self.queue.pop(position)
                self.queue.insert(0, e)

            callbacks += self.__prefetch()

        await _invoke(callbacks)

    def read(self) -> AudioChunk:
        callbacks = []
        with self.lock:
            # print("[ ] YTDLQueuedStreamAudio read")
            if not self.queue:
                print("[ ] queue empty")
                return AudioChunk(data=b"", playback_id=-1)
            source = self.queue[0]
            c = source.read()
            if source.playback_id is None:
                self.current_playback_id += 1
                source.playback_id = self.current_playback_id
            playback_id = source.playback_id
            # print(f"[ ] YTDLQueuedStreamAudio got {len(c)} bytes from queue head")
            if len(c) < self.read_size:
                if len(self.queue) > 1:
                    c = c + self.zeros[len(c) :]
                callbacks.append(source.cleanup)
                print("[ ] advancing queue")
                self.queue = self.queue[1:]
                if len(self.queue) >= 2:
                    callbacks.append(self.queue[1].prefetch)
        for i in callbacks:
            i()
        return AudioChunk(c, playback_id)

    def cleanup(self) -> None:
        with self.lock:
            print("[ ] YTDLQueuedStreamAudio cleanup")
            for a in self.queue:
                cast(YTDLStreamAudio, a).cleanup()
            self.queue = []

    def current_track_id(self) -> int | None:
        with self.lock:
            if not self.queue:
                return None
            return self.queue[0].track.track_id

    def current_position(self, track_id: int) -> int | None:
        with self.lock:
            return self.__current_position(track_id)

    def __current_position(self, track_id: int) -> int | None:
        for i, e in enumerate(self.queue):
            if e.track.track_id == track_id:
                return i
        return None

    def __prefetch(self) -> list[Callable[[], None]]:
        callbacks = []
        if len(self.queue) >= 1:
            callbacks.append(self.queue[0].prefetch)
        if len(self.queue) >= 2:
            callbacks.append(self.queue[1].prefetch)
        if len(self.queue) >= 3:
            callbacks.append(self.queue[2].cleanup)
        return callbacks


class BufferedAudioSource(discord.AudioSource):
    def __init__(self, source: YTDLQueuedStreamAudio, executor: Executor) -> None:
        self.done = False
        self.max_chunk_count = 256
        self.preload_chunk_count = 128
        self.source = source
        self.executor = executor
        self.chunks: list[AudioChunk] = []
        self.access_sem = threading.Lock()
        self.chunk_sem = threading.Semaphore(value=0)
        self.cv = threading.Condition(self.access_sem)
        self.chunks_to_skip = 0
        self.future = self.executor.submit(self.__fetcher_task)
        self.current_playback_id = -1
        self.min_playback_id = -1
        with self.chunk_sem:
            pass

    def drain(self) -> None:
        with self.access_sem:
            self.min_playback_id = self.current_playback_id + 1

    def is_done(self) -> bool:
        with self.access_sem:
            return self.done

    @override
    def read(self) -> bytes:
        # print("[ ] BufferedAudioSource read")
        if not self.chunk_sem.acquire(timeout=0.010):
            return b"\0" * 3840
        self.access_sem.acquire()

        if not self.chunks:
            print("[ ] BufferedAudioSource finished")
            self.chunk_sem.release()
            self.access_sem.release()
            return b""

        c = self.chunks[0]
        self.current_playback_id = c.playback_id
        self.chunks = self.chunks[1:]
        if len(self.chunks) == self.max_chunk_count - 1:
            self.cv.notify()

        if c.playback_id < self.min_playback_id:
            self.access_sem.release()
            return self.read()

        self.access_sem.release()
        return c.data

    @override
    def cleanup(self) -> None:
        print("[ ] BufferedAudioSource cleanup")
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

            chunk = self.source.read()
            if len(chunk.data) == 0:
                self.chunk_sem.release(chunks_pending + 1)
                self.done = True
                print("[ ] BufferedAudioSource fetcher stopped, finished")
                break
            with self.access_sem:
                self.chunks.append(chunk)
            chunks_pending += 1


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(
        self, queue: YTDLQueuedStreamAudio, volume: float, executor: Executor
    ) -> None:
        self.buffered_audio = BufferedAudioSource(queue, executor)
        super().__init__(self.buffered_audio, volume)
