import subprocess
import threading
import asyncio
import dataclasses
import tempfile
import inspect
from typing import override, Callable, Awaitable, Sequence, MutableSequence
import io
from concurrent.futures import Executor
from discord.opus import Encoder as OpusEncoder
import discord
import zmq
from .util import add_to_embed, PlaybackOptions

AUDIO_PACKET_SIZE = 3840


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
            "yt-dlp",
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
    interaction: discord.Interaction
    on_enqueue: Callable[[], Awaitable[None]] | None = None
    on_dequeue: Callable[[], Awaitable[None]] | None = None
    lock = asyncio.Lock()
    on_enqueue_time: int | None = None
    can_edit_message: bool = True


class YTDLStreamAudio(discord.FFmpegAudio):
    def __init__(self, url: str, options: PlaybackOptions) -> None:
        self._buffer = YTDLBuffer(url, options)
        self._temporary_file = tempfile.NamedTemporaryFile()

        filter_graph = f"azmq=b=ipc\\\\://{self._temporary_file.name},"

        nightcore_factor = (
            options.nightcore_factor if options.nightcore_factor is not None else 1.0
        )
        filter_graph += (
            f"rubberband@1=tempo={nightcore_factor}:pitch={nightcore_factor}"
        )

        bassboost_factor = (
            options.bassboost_factor if options.bassboost_factor is not None else 0.0
        )
        filter_graph += f",bass@1=g={bassboost_factor}"

        filter_graph += (
            f",volume@1=volume={options.volume if options.volume is not None else 1.0}"
        )

        if options.filter_graph is not None:
            filter_graph += f",{options.filter_graph}"

        super().__init__(
            source=self._buffer,
            executable="ffmpeg",
            args=[
                "-i",
                "-",
                "-f",
                "s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-blocksize",
                "8192",
                "-vn",
                "-loglevel",
                "error",
                "-af",
                filter_graph,
                "-",
            ],
            stdin=subprocess.PIPE,
        )

    @override
    def read(self) -> bytes:
        ret = self._stdout.read(OpusEncoder.FRAME_SIZE)
        if len(ret) != OpusEncoder.FRAME_SIZE:
            return b""
        return ret

    @override
    def cleanup(self) -> None:
        try:
            print("[ ] YTDLStreamAudio cleanup")
            self._buffer.cleanup()
            self._temporary_file.close()
            super().cleanup()
        except ValueError as e:
            print(f"[ ] YTDLStreamAudio cleanup {e}")
        except AttributeError as e:
            print(f"[ ] YTDLStreamAudio cleanup {e}")

    @staticmethod
    def __send_string(socket: zmq.SyncSocket, command: str) -> None:
        socket.send_string(command)
        response = socket.recv_string()
        if response != "0 Undefined error: 0":
            raise RuntimeError(f"Failed to send zmq command. {response}")

    def set_options(self, options: PlaybackOptions) -> None:
        with zmq.Context() as context:
            socket = context.socket(zmq.REQ)
            with socket.connect(f"ipc://{self._temporary_file.name}"):
                if options.nightcore_factor:
                    self.__send_string(
                        socket, f"rubberband@1 tempo {options.nightcore_factor}"
                    )
                    self.__send_string(
                        socket, f"rubberband@1 pitch {options.nightcore_factor}"
                    )
                if options.bassboost_factor:
                    self.__send_string(socket, f"bass@1 g {options.bassboost_factor}")
                if options.volume:
                    self.__send_string(socket, f"volume@1 volume {options.volume}")


class LazyAudioSource(discord.AudioSource):
    def __init__(self, track: AudioTrack) -> None:
        self.track = track
        self.source: YTDLStreamAudio | None = None
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


async def _invoke(callbacks: Sequence[Callable[[], None] | Awaitable[None]]):
    async_funcs = [i for i in callbacks if inspect.iscoroutine(i)]
    sync_funcs = [i for i in callbacks if not inspect.iscoroutine(i)]

    def f():
        for i in sync_funcs:
            i()

    await asyncio.gather(asyncio.to_thread(f), *async_funcs)


@dataclasses.dataclass
class AudioChunk:
    data: bytes
    playback_id: int


class YTDLQueuedStreamAudio:
    def __init__(
        self,
        executor: Executor,
        on_enqueued: Callable[[AudioTrack], Awaitable[None]],
        on_dequeued: Callable[[AudioTrack], Awaitable[None]],
    ) -> None:
        super().__init__()
        self.main_loop = asyncio.get_event_loop()
        self.executor = executor
        self.queue: list[LazyAudioSource] = []
        self.lock = threading.Lock()
        self.zeros = b"\0" * AUDIO_PACKET_SIZE
        self.current_playback_id = 0
        self.on_enqueued = on_enqueued
        self.on_dequeued = on_dequeued

    async def add(self, track: AudioTrack) -> None:
        callbacks: MutableSequence[Callable[[], None] | Awaitable[None]] = []
        with self.lock:
            print(f"[ ] adding {track.url} to queue")
            source = LazyAudioSource(track)
            self.queue.append(source)
            if len(self.queue) == 1:
                callbacks.append(self.on_enqueued(source.track))
            callbacks += self.__prefetch()
        await _invoke(callbacks)

    async def skip(self, track_id: int | None = None) -> None:
        callbacks: MutableSequence[Callable[[], None] | Awaitable[None]] = []
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
            callbacks.append(self.on_dequeued(self.queue[d].track))
            self.queue.pop(d)
            if d == 0 and len(self.queue) > 0:
                callbacks.append(self.on_enqueued(self.queue[0].track))

            callbacks += self.__prefetch()

        await _invoke(callbacks)

    async def play_now(
        self,
        track: AudioTrack,
    ) -> None:
        callbacks: MutableSequence[Callable[[], None] | Awaitable[None]] = []
        with self.lock:
            position = self.__current_position(track.track_id)
            if position == 0:
                return

            if self.queue:
                callbacks.append(self.on_dequeued(self.queue[0].track))
                callbacks.append(self.queue[0].cleanup)
                self.queue.pop(0)

            if position is None:
                e = LazyAudioSource(track)
                self.queue.insert(0, e)
                callbacks.append(self.on_enqueued(e.track))
            elif position > 1:
                position -= 1
                e = self.queue[position]
                self.queue.pop(position)
                self.queue.insert(0, e)
                callbacks.append(self.on_enqueued(e.track))

            callbacks += self.__prefetch()

        await _invoke(callbacks)

    def read(self) -> AudioChunk:
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
            if len(c) < AUDIO_PACKET_SIZE:
                print("[ ] advancing queue")
                if len(self.queue) > 1:
                    c = c + self.zeros[len(c) :]
                self.executor.submit(source.cleanup)
                self.queue = self.queue[1:]
                asyncio.ensure_future(
                    self.on_dequeued(source.track), loop=self.main_loop
                )
                if len(self.queue) >= 1:
                    asyncio.ensure_future(
                        self.on_enqueued(self.queue[0].track), loop=self.main_loop
                    )
                if len(self.queue) >= 2:
                    self.executor.submit(self.queue[1].prefetch)
        return AudioChunk(c, playback_id)

    def cleanup(self) -> None:
        with self.lock:
            print("[ ] YTDLQueuedStreamAudio cleanup")
            for a in self.queue:
                asyncio.ensure_future(self.on_dequeued(a.track))
                self.executor.submit(a.cleanup)
            self.queue = []

    def current_track_id(self) -> int | None:
        with self.lock:
            if not self.queue:
                return None
            return self.queue[0].track.track_id

    async def __update_embed(
        self, interaction: discord.Interaction, options: PlaybackOptions
    ) -> None:
        message = await interaction.original_response()
        embed = message.embeds[0]
        add_to_embed(embed, options)
        await interaction.edit_original_response(embeds=[embed], attachments=[])

    def set_options(self, options: PlaybackOptions) -> None:
        with self.lock:
            if not self.queue or not self.queue[0].source:
                return
            source = self.queue[0].source
            self.executor.submit(source.set_options, options)
            asyncio.ensure_future(
                self.__update_embed(self.queue[0].track.interaction, options),
                loop=self.main_loop,
            )

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
        self.max_chunk_count = 32
        self.preload_chunk_count = 16
        self.source = source
        self.executor = executor
        self.chunks: list[AudioChunk] = []
        self.access_sem = threading.Lock()
        self.chunk_sem = threading.Semaphore(value=0)
        self.cv = threading.Condition(self.access_sem)
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
            return b"\0" * AUDIO_PACKET_SIZE
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

            chunk = self.source.read()
            if len(chunk.data) == 0:
                self.chunk_sem.release(chunks_pending + 1)
                self.done = True
                print("[ ] BufferedAudioSource fetcher stopped, finished")
                break
            with self.access_sem:
                self.chunks.append(chunk)
                chunks_pending += 1
                if len(self.chunks) >= self.preload_chunk_count:
                    self.chunk_sem.release(chunks_pending)
                    chunks_pending = 0


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(
        self, queue: YTDLQueuedStreamAudio, volume: float, executor: Executor
    ) -> None:
        self.buffered_audio = BufferedAudioSource(queue, executor)
        super().__init__(self.buffered_audio, volume)
