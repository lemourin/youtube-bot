import asyncio
import tempfile
from typing import cast, Dict, Any, Tuple
from types import TracebackType
import io
from functools import reduce
import uuid
import pathlib
import os
import sys
import json
import functools
from urllib.parse import urlparse
from concurrent.futures import Executor, ThreadPoolExecutor
import subprocess
import dataclasses
import aiohttp
import aiohttp.web
import discord
import discord.ext.commands
import validators
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
    audio_filter_graph,
    ytdl_time_range,
    JellyfinLibraryClient,
    SearchEntry,
    MessageContent,
    PlaybackOptions,
    FileStorageOptions,
)


@dataclasses.dataclass
class InlineAttachment:
    content: io.BufferedIOBase
    ext: str

    def __enter__(self):
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ):
        self.content.close()


@dataclasses.dataclass
class Attachment:
    title: str
    url: str | None = None
    inline_attachment: InlineAttachment | None = None

    def __enter__(self):
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ):
        if self.inline_attachment is not None:
            self.inline_attachment.__exit__(exc_type, exc_value, traceback)


def timestamp_to_seconds(timestamp: str) -> int:
    nums = list(reversed([int(x) for x in timestamp.split(":")]))
    if len(nums) > 3:
        raise discord.ext.commands.CommandError("Invalid timestamp!")
    print(nums)
    ts = 0
    if len(nums) >= 1:
        ts += nums[0]
    if len(nums) >= 2:
        ts += 60 * nums[1]
    if len(nums) >= 3:
        ts += 3600 * nums[2]
    return ts


def _create_file(storage_path: str | None, extension: str):
    if storage_path:
        file = tempfile.NamedTemporaryFile(
            dir=storage_path, suffix=f".{extension}", delete=False
        )
        os.chmod(file.fileno(), mode=0o644)
        return file
    else:
        return tempfile.TemporaryFile()


def _transcode_h264_pass_1(input_fd: int, pass_log_file: str, bitrate_v: int):
    with subprocess.Popen(
        args=[
            "ffmpeg",
            "-fd",
            f"{input_fd}",
            "-i",
            "fd:",
            "-f",
            "mp4",
            "-y",
            "-passlogfile",
            pass_log_file,
            "-pass",
            "1",
            "-b:v",
            f"{bitrate_v}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "2",
            "-x264-params",
            "threads=2",
            "-an",
            "-f",
            "null",
            f"{"NUL" if os.name == "nt" else "/dev/null"}",
        ],
        stdout=sys.stdout,
        pass_fds=[input_fd],
    ) as proc:
        try:
            proc.wait(timeout=300)
            if proc.returncode != 0:
                raise discord.ext.commands.CommandError(
                    f"ffmpeg error {proc.returncode} on encode pass 1"
                )
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise discord.ext.commands.CommandError(
                f"ffmpeg encode pass 1 deadline exceeded"
            )


def _transcode_h264_pass_2(
    input_fds: list[int],
    output_fd: int,
    pass_log_file: str,
    bitrate_v: int,
    bitrate_a: int,
    graph: str | None,
):
    with subprocess.Popen(
        args=[
            "ffmpeg",
        ]
        + sum((["-fd", f"{fd}", "-i", "fd:"] for fd in input_fds), [])
        + [
            "-f",
            "mp4",
            "-y",
            "-passlogfile",
            pass_log_file,
            "-pass",
            "2",
            "-b:v",
            f"{bitrate_v}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "2",
            "-x264-params",
            "threads=2",
            "-c:a",
            "aac",
            "-b:a",
            f"{bitrate_a}",
        ]
        + (["-af", graph] if graph else [])
        + ["-fd", f"{output_fd}", "fd:"],
        stdout=sys.stdout,
        pass_fds=input_fds + [output_fd],
    ) as proc:
        try:
            proc.wait(timeout=450)
            if proc.returncode != 0:
                raise discord.ext.commands.CommandError(
                    f"ffmpeg error {proc.returncode} on transcode"
                )
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise discord.ext.commands.CommandError(
                f"ffmpeg encode pass 2 deadline exceeded"
            )


def _transcode_h265_pass_1(input_fd: int, pass_log_file: str, bitrate_v: int):
    with subprocess.Popen(
        args=[
            "ffmpeg",
            "-fd",
            f"{input_fd}",
            "-i",
            "fd:",
            "-f",
            "mp4",
            "-y",
            "-b:v",
            f"{bitrate_v}",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-tag:v",
            "hvc1",
            "-x265-params",
            f"stats={pass_log_file}:pass=1:frame-threads=2:pools=4",
            "-an",
            "-f",
            "null",
            f"{"NUL" if os.name == "nt" else "/dev/null"}",
        ],
        pass_fds=[input_fd],
    ) as proc:
        try:
            proc.wait(timeout=300)
            if proc.returncode != 0:
                raise discord.ext.commands.CommandError(
                    f"ffmpeg error {proc.returncode} on encode pass 1"
                )
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise discord.ext.commands.CommandError(
                f"ffmpeg encode pass 1 deadline exceeded"
            )


def _transcode_h265_pass_2(
    input_fds: list[int],
    output_fd: int,
    pass_log_file: str,
    bitrate_v: int,
    bitrate_a: int,
    graph: str | None,
):
    with subprocess.Popen(
        args=["ffmpeg"]
        + sum((["-fd", f"{input_fd}", "-i", "fd:"] for input_fd in input_fds), [])
        + [
            "-f",
            "mp4",
            "-y",
            "-b:v",
            f"{bitrate_v}",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-tag:v",
            "hvc1",
            "-c:a",
            "aac",
            "-b:a",
            f"{bitrate_a}",
            "-x265-params",
            f"stats={pass_log_file}:pass=2:frame-threads=2:pools=4",
        ]
        + (["-af", graph] if graph else [])
        + ["-fd", f"{output_fd}", "fd:"],
        pass_fds=input_fds + [output_fd],
    ) as proc:
        try:
            proc.wait(timeout=450)
            if proc.returncode != 0:
                raise discord.ext.commands.CommandError(
                    f"ffmpeg error {proc.returncode} on transcode"
                )
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise discord.ext.commands.CommandError(
                f"ffmpeg encode pass 2 deadline exceeded"
            )


def _transcode_webp(input_fd: int, output_fd: int, bitrate_v: int):
    with subprocess.Popen(
        args=[
            "ffmpeg",
            "-fd",
            f"{input_fd}",
            "-i",
            "fd:",
            "-f",
            "webp",
            "-y",
            "-b:v",
            f"{bitrate_v}",
            "-c:v",
            "webp",
            "-vf",
            "scale=320:-2",
            "-an",
        ]
        + ["-fd", f"{output_fd}", "fd:"],
        pass_fds=[input_fd, output_fd],
    ) as proc:
        try:
            proc.wait(timeout=600)
            if proc.returncode != 0:
                raise discord.ext.commands.CommandError(
                    f"ffmpeg error {proc.returncode} on transcode"
                )
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise discord.ext.commands.CommandError(
                f"ffmpeg webp transcode deadline exceeded"
            )


def _content_duration_seconds(
    duration_seconds: int | None, options: PlaybackOptions
) -> int:
    if not duration_seconds:
        raise discord.ext.commands.CommandError("Duration unknown!")

    if options.start_timestamp is not None and options.stop_timestamp is not None:
        duration_seconds = (
            timestamp_to_seconds(options.stop_timestamp)
            - timestamp_to_seconds(options.start_timestamp)
            + 1
        )
    elif options.start_timestamp is not None:
        duration_seconds -= timestamp_to_seconds(options.start_timestamp)
    elif options.stop_timestamp is not None:
        duration_seconds = timestamp_to_seconds(options.stop_timestamp) + 1

    if duration_seconds <= 1:
        raise discord.ext.commands.CommandError("Invalid duration!")

    return duration_seconds


def _supported_codecs():
    return ["h264", "h265", "hvc1", "avc1", "vp09"]


@dataclasses.dataclass
class FetchResult:
    title: str
    input_codec_v: str
    input_filename_v: str
    input_filename_a: str | None
    input_bitrate_v: int
    input_bitrate_a: int
    duration_seconds: int


def _yt_dlp_fetch(
    url: str,
    options: PlaybackOptions,
    storage_options: FileStorageOptions,
    filesize_limit: int,
    uuid: str,
) -> FetchResult:
    max_video_size = 0.8 * filesize_limit
    max_video_dl_size = storage_options.attachment.max_video_dl_size
    max_audio_dl_size = storage_options.attachment.max_audio_dl_size
    supported_vcodec = f"vcodec~='^({"|".join(_supported_codecs())})'"
    format_str = "/".join(
        [
            f"bv[filesize<{max_video_size}][{supported_vcodec}],ba[filesize<{max_audio_dl_size}]",
            f"bv[filesize_approx<{max_video_size}][{supported_vcodec}],ba[filesize_approx<{max_audio_dl_size}]",
            f"b[filesize<{max_video_size}][{supported_vcodec}]",
            f"b[filesize_approx<{max_video_size}][{supported_vcodec}]",
            f"bv[filesize<{max_video_dl_size}],ba[filesize<{max_audio_dl_size}]",
            f"bv[filesize_approx<{max_video_dl_size}],ba[filesize_approx<{max_audio_dl_size}]",
            f"b[filesize<{max_video_dl_size + max_audio_dl_size}]",
            f"b[filesize_approx<{max_video_dl_size + max_audio_dl_size}]",
            f"wv[{supported_vcodec}],wa",
            f"w[{supported_vcodec}]",
            "wv,wa",
            "w",
            f"bv[filesize<{max_video_size}][{supported_vcodec}]",
            f"bv[filesize_approx<{max_video_size}][{supported_vcodec}]",
            f"bv[filesize<{max_video_dl_size}][{supported_vcodec}]",
            f"bv[filesize_approx<{max_video_dl_size}][{supported_vcodec}]",
            f"bv[filesize<{max_video_dl_size}]",
            f"bv[filesize_approx<{max_video_dl_size}]",
            f"ba[filesize<{max_audio_dl_size}]",
            f"ba[filesize_approx<{max_audio_dl_size}]",
            "wv",
            "wa",
        ]
    )
    time_range = ytdl_time_range(options)
    with subprocess.Popen(
        args=[
            "yt-dlp",
            "--cookies",
            "cookie.txt",
            "-f",
            format_str,
            "--max-filesize",
            f"{max_video_dl_size + max_audio_dl_size}",
            "-J",
            "--no-simulate",
            "-o",
            f"{pathlib.Path(storage_options.tmp_file_path) / str(uuid)}.%(ext)s",
            url,
        ]
        + (["--download-sections", f"*{time_range}"] if time_range else []),
        stdout=subprocess.PIPE,
        text=True,
    ) as proc:
        try:
            stdout, _ = proc.communicate(timeout=120)
            if proc.returncode != 0:
                raise discord.ext.commands.CommandError(
                    f"yt-dlp error {proc.returncode}"
                )
            yt_dlp_info = json.loads(stdout)

            input_codec_v = None
            input_filename_v = None
            input_filename_a = None
            input_bitrate_v = None
            input_bitrate_a = None
            for d in yt_dlp_info["requested_downloads"]:
                if input_filename_v is None and d["vcodec"] != "none":
                    input_filename_v = d["filepath"]
                    input_codec_v = d["vcodec"]
                    if d["acodec"] != "none":
                        input_bitrate_v = (
                            d["tbr"] * 1000 if "tbr" in d else 8 * max_video_dl_size
                        )
                        input_bitrate_a = 0
                    else:
                        input_bitrate_v = (
                            d["vbr"] * 1000 if "vbr" in d else 8 * max_video_dl_size
                        )
                elif (
                    input_filename_a is None
                    and input_bitrate_a is None
                    and d["vcodec"] == "none"
                    and d["acodec"] != "none"
                ):
                    input_filename_a = d["filepath"]
                    input_bitrate_a = (
                        d["abr"] * 1000 if "abr" in d else 8 * max_audio_dl_size
                    )

            if not (input_codec_v and input_filename_v and input_bitrate_v):
                raise discord.ext.commands.CommandError("Video source not found!")
            if input_bitrate_a is None:
                raise discord.ext.commands.CommandError("No audio found!")

            return FetchResult(
                title=yt_dlp_info.get("title") or "No title.",
                input_codec_v=input_codec_v,
                input_filename_v=input_filename_v,
                input_filename_a=input_filename_a,
                input_bitrate_v=input_bitrate_v,
                input_bitrate_a=input_bitrate_a,
                duration_seconds=_content_duration_seconds(
                    yt_dlp_info.get("duration"), options
                ),
            )

        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise discord.ext.commands.CommandError(f"yt-dlp fetch deadline exceeded")


def _two_pass_transcode(
    video_codec: str,
    options: PlaybackOptions,
    pass_log_file: str,
    bitrate_v: int,
    bitrate_a: int,
    video_fd: int,
    audio_fd: int | None,
    output_fd: int,
):
    transcode_pass_1 = (
        _transcode_h265_pass_1 if (video_codec == "h265") else _transcode_h264_pass_1
    )
    transcode_pass_2 = (
        _transcode_h265_pass_2 if (video_codec == "h265") else _transcode_h264_pass_2
    )
    transcode_pass_1(video_fd, pass_log_file, bitrate_v)
    os.lseek(video_fd, 0, os.SEEK_SET)
    transcode_pass_2(
        input_fds=[video_fd] + [audio_fd] if audio_fd is not None else [],
        output_fd=output_fd,
        pass_log_file=pass_log_file,
        bitrate_v=bitrate_v,
        bitrate_a=bitrate_a,
        graph=audio_filter_graph(options),
    )


def _remux_video(
    format: str,
    input_fds: list[int],
    output_fd: int,
    bitrate_a: int,
    audio_codec: str,
    graph: str | None,
):
    with subprocess.Popen(
        args=["ffmpeg"]
        + sum((["-fd", f"{fd}", "-i", "fd:"] for fd in input_fds), [])
        + [
            "-f",
            format,
            "-y",
            "-c:v",
            "copy",
            "-c:a",
            audio_codec,
            "-b:a",
            f"{bitrate_a}",
        ]
        + (["-af", graph] if graph else [])
        + ["-fd", f"{output_fd}", "fd:"],
        pass_fds=input_fds + [output_fd],
    ) as proc:
        try:
            proc.wait(timeout=600)
            if proc.returncode != 0:
                raise discord.ext.commands.CommandError(
                    f"ffmpeg error {proc.returncode} on remux"
                )
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise discord.ext.commands.CommandError(f"ffmpeg remux deadline exceeded")


def _needs_video_transcode(
    vcodec: str,
    input_bitrate_v: int,
    input_bitrate_a: int,
    duration_seconds: int,
    filesize_limit: int,
):
    if (input_bitrate_v + input_bitrate_a) * duration_seconds > 8 * filesize_limit:
        return True
    for c in _supported_codecs():
        if vcodec.startswith(c):
            return False
    return True


def _convert_video(
    input_uuid: str,
    extension: str,
    options: PlaybackOptions,
    storage_options: FileStorageOptions,
    input_codec_v: str,
    dest_codec_v: str,
    input_bitrate_v: int,
    input_bitrate_a: int,
    bitrate_v: int,
    bitrate_a: int,
    duration_seconds: int,
    filesize_limit: int,
    input_fd_v: int,
    input_fd_a: int | None,
    output_fd: int,
) -> str:
    if extension == "mp4":
        if _needs_video_transcode(
            vcodec=input_codec_v,
            input_bitrate_v=input_bitrate_v,
            input_bitrate_a=bitrate_a,
            duration_seconds=duration_seconds,
            filesize_limit=filesize_limit,
        ):
            _two_pass_transcode(
                video_codec=dest_codec_v,
                options=options,
                pass_log_file=str(
                    pathlib.Path(storage_options.tmp_file_path) / f"{input_uuid}.log"
                ),
                bitrate_v=bitrate_v,
                bitrate_a=bitrate_a,
                video_fd=input_fd_v,
                audio_fd=input_fd_a,
                output_fd=output_fd,
            )
            return "mp4"
        else:
            format = "webm" if input_codec_v.startswith("vp09") else "mp4"
            _remux_video(
                format=format,
                input_fds=[input_fd_v] + ([input_fd_a] if input_fd_a else []),
                output_fd=output_fd,
                bitrate_a=bitrate_a,
                audio_codec="libopus" if format == "webm" else "aac",
                graph=options.filter_graph,
            )
            return format
    else:  # webp
        _transcode_webp(
            input_fd=input_fd_v,
            output_fd=output_fd,
            bitrate_v=bitrate_v,
        )
        return "webp"


def extract_content(
    url: str,
    options: PlaybackOptions,
    filesize_limit: int,
    storage_options: FileStorageOptions,
    extension: str = "mp4",
    video_codec: str = "h265",
) -> Attachment:
    input_uuid = uuid.uuid4()
    input_file_v = None
    input_file_a = None
    output_file = None
    try:
        info = _yt_dlp_fetch(
            url, options, storage_options, filesize_limit, str(input_uuid)
        )
        title = info.title
        input_codec_v = info.input_codec_v
        input_filename_v = info.input_filename_v
        input_filename_a = info.input_filename_a
        duration_seconds = info.duration_seconds

        bitrate_a = storage_options.attachment.audio_bitrate
        bitrate_v = min(
            8 * filesize_limit // duration_seconds - bitrate_a,
            storage_options.attachment.max_video_bitrate,
        )
        if bitrate_v < storage_options.attachment.min_video_bitrate:
            raise discord.ext.commands.CommandError("File too big!")

        input_file_v = open(input_filename_v)
        input_file_a = open(input_filename_a) if input_filename_a else None
        output_file = _create_file(storage_options.storage_path, extension)
        extension = _convert_video(
            input_uuid=str(input_uuid),
            extension=extension,
            options=options,
            storage_options=storage_options,
            input_codec_v=input_codec_v,
            dest_codec_v=video_codec,
            input_bitrate_v=info.input_bitrate_v,
            input_bitrate_a=info.input_bitrate_a,
            bitrate_v=bitrate_v,
            bitrate_a=bitrate_a,
            duration_seconds=duration_seconds,
            filesize_limit=filesize_limit,
            input_fd_v=input_file_v.fileno(),
            input_fd_a=input_file_a.fileno() if input_file_a else None,
            output_fd=output_file.fileno(),
        )

        if storage_options.url_path:
            url = f"{storage_options.url_path}{os.path.basename(output_file.name)}"
            output_file.close()
            output_file = None
            return Attachment(title=title, url=url)
        else:
            output_file.seek(0)
            return Attachment(
                title=title,
                inline_attachment=InlineAttachment(content=output_file, ext=extension),  # type: ignore
            )
    except:
        if output_file:
            output_file.close()
        raise
    finally:
        if input_file_v:
            input_file_v.close()
        if input_file_a:
            input_file_a.close()

        for path in os.scandir(storage_options.tmp_file_path):
            if str(input_uuid) in path.path:
                os.remove(path)


class DiscordCog(discord.ext.commands.Cog):
    def __init__(
        self,
        bot: discord.ext.commands.Bot,
        executor: Executor,
        http: aiohttp.ClientSession,
        discord_admin_id: int,
        file_storage_options: FileStorageOptions,
        jellyfin_client: JellyfinLibraryClient | None = None,
        youtube_client: Any = None,
    ) -> None:
        self.bot = bot
        self.executor = executor
        self.http = http
        self.discord_admin_id = discord_admin_id
        self.file_storage_options = file_storage_options
        self.state: Dict[int, GuildState] = {}
        self.jellyfin_client = jellyfin_client
        self.youtube_client = youtube_client
        self.next_track_id = 0
        self.transcode_executor = ThreadPoolExecutor(max_workers=2)

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
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=False, private_channel=False
                ),
            ),
            discord.app_commands.Command(
                name="jf",
                description="Play audio of a video sourced from a Jellyfin server.",
                callback=self.jf,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=False, private_channel=False
                ),
            ),
            discord.app_commands.Command(
                name="join",
                description="Add bot to the voice channel.",
                callback=self.join,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=False, private_channel=False
                ),
            ),
            discord.app_commands.Command(
                name="skip",
                description="Skip currently playing audio.",
                callback=self.skip,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=False, private_channel=False
                ),
            ),
            discord.app_commands.Command(
                name="volume",
                description="Change volume of the currently playing audio.",
                callback=self.volume,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=False, private_channel=False
                ),
            ),
            discord.app_commands.Command(
                name="stop",
                description="Pause audio playback.",
                callback=self.stop,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=False, private_channel=False
                ),
            ),
            discord.app_commands.Command(
                name="leave",
                description="Disconnect bot from the voice channel.",
                callback=self.leave,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=False, private_channel=False
                ),
            ),
            discord.app_commands.Command(
                name="die",
                description="Kill the bot.",
                callback=self.die,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=True, private_channel=True
                ),
            ),
            discord.app_commands.Command(
                name="ping",
                description="Ping.",
                callback=self.ping,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=True, private_channel=True
                ),
            ),
            discord.app_commands.Command(
                name="queue",
                description="Show enqueued tracks.",
                callback=self.queue,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=False, private_channel=False
                ),
            ),
            discord.app_commands.Command(
                name="attach",
                description="Extract a video out of a url and post an embed with it.",
                callback=self.attach,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=True, private_channel=True
                ),
            ),
            discord.app_commands.Command(
                name="attach_gif",
                description="Extract a video out of a url and post a GIF with it.",
                callback=self.attach_gif,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=True, private_channel=True
                ),
            ),
            discord.app_commands.Command(
                name="set",
                description="Alter properties of the currently playing audio.",
                callback=self.set,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=False, private_channel=False
                ),
            ),
            discord.app_commands.Command(
                name="nightcore",
                description="Apply nightcore filter to currently playing audio.",
                callback=self.nightcore,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=False, private_channel=False
                ),
            ),
            discord.app_commands.Command(
                name="bassboost",
                description="Apply bassboost filter to currently playing audio.",
                callback=self.bassboost,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=False, private_channel=False
                ),
            ),
            discord.app_commands.Command(
                name="denightcore",
                description="Negate nightcore filter applied to currently playing audio.",
                callback=self.denightcore,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=False, private_channel=False
                ),
            ),
            discord.app_commands.Command(
                name="debassboost",
                description="Negate bassboost filter applied to currently playing audio.",
                callback=self.debassboost,
                allowed_contexts=discord.app_commands.AppCommandContext(
                    guild=True, dm_channel=False, private_channel=False
                ),
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
                    preview: Any = await asyncio.to_thread(
                        link_preview,
                        url=url,
                        content=str(await read_at_most(data.content, 1024 * 1024)),
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
        nightcore_factor=PlaybackOptions.NIGHTCORE_FACTOR_DOC,
        bassboost_factor=PlaybackOptions.BASSBOOST_FACTOR_DOC,
        filter_graph=PlaybackOptions.FILTER_GRAPH_DOC,
        start_timestamp=PlaybackOptions.START_TIMESTAMP_DOC,
        stop_timestamp=PlaybackOptions.STOP_TIMESTAMP_DOC,
        volume=PlaybackOptions.VOLUME_DOC,
        video_codec="Video codec to be used. Use h264 for quick transcode, h265 for high quality.",
    )
    async def attach(
        self,
        interaction: discord.Interaction,
        url: str,
        nightcore_factor: float | None,
        bassboost_factor: float | None,
        filter_graph: str | None,
        start_timestamp: str | None,
        stop_timestamp: str | None,
        volume: int | None,
        video_codec: str | None,
    ) -> None:
        await self.__attach(
            interaction,
            url,
            nightcore_factor=nightcore_factor,
            bassboost_factor=bassboost_factor,
            filter_graph=filter_graph,
            start_timestamp=start_timestamp,
            stop_timestamp=stop_timestamp,
            volume=volume,
            extension="mp4",
            video_codec=video_codec if video_codec else "h265",
        )

    @discord.app_commands.describe(
        url="A url to extract a video from.",
        start_timestamp=PlaybackOptions.START_TIMESTAMP_DOC,
        stop_timestamp=PlaybackOptions.STOP_TIMESTAMP_DOC,
    )
    async def attach_gif(
        self,
        interaction: discord.Interaction,
        url: str,
        start_timestamp: str | None,
        stop_timestamp: str | None,
    ) -> None:
        await self.__attach(
            interaction,
            url,
            start_timestamp=start_timestamp,
            stop_timestamp=stop_timestamp,
            extension="webp",
            video_codec="webp",
        )

    async def __attach(
        self,
        interaction: discord.Interaction,
        url: str,
        nightcore_factor: float | None = None,
        bassboost_factor: float | None = None,
        filter_graph: str | None = None,
        start_timestamp: str | None = None,
        stop_timestamp: str | None = None,
        volume: int | None = None,
        extension: str = "mp4",
        video_codec: str = "h265",
    ) -> None:
        await interaction.response.defer()
        try:
            if extension == "mp4" and video_codec not in ["h264", "h265"]:
                raise discord.ext.commands.CommandError("Video codec not supported.")
            elif extension == "webp" and video_codec not in ["webp"]:
                raise discord.ext.commands.CommandError("Video codec not supported.")

            filesize_limit = (
                self.file_storage_options.attachment.max_size
                if self.file_storage_options.attachment.max_size is not None
                and self.file_storage_options.url_path is not None
                else (
                    interaction.guild.filesize_limit
                    if interaction.guild
                    else discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES
                )
            )
            with await asyncio.get_running_loop().run_in_executor(
                self.transcode_executor,
                lambda: extract_content(
                    url,
                    options=PlaybackOptions(
                        nightcore_factor=nightcore_factor,
                        bassboost_factor=bassboost_factor,
                        filter_graph=filter_graph,
                        start_timestamp=start_timestamp,
                        stop_timestamp=stop_timestamp,
                        volume=min(max(volume, 0), 200) / 100 if volume else None,
                    ),
                    filesize_limit=filesize_limit,
                    storage_options=self.file_storage_options,
                    extension=extension,
                    video_codec=video_codec,
                ),
            ) as attachment:
                if attachment.inline_attachment is not None:
                    await interaction.followup.send(
                        content=f"## {attachment.title}",
                        file=discord.File(
                            attachment.inline_attachment.content,
                            filename=f"file.{attachment.inline_attachment.ext}",
                        ),
                    )
                elif attachment.url is not None:
                    await interaction.followup.send(content=attachment.url)
                else:
                    await interaction.followup.send("Failed to extract a video.")
        except discord.ext.commands.CommandError as e:
            await interaction.followup.send(e.args[0])

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
                await button_interaction.response.send_message(
                    "Fuck off.", ephemeral=True, delete_after=5
                )
                return
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
        commands = await self.bot.tree.sync(guild=guild)
        await ctx.send(content=f"Synced {len(commands)} commands.")

    @discord.ext.commands.command()
    async def unsync(self, ctx: discord.ext.commands.Context) -> None:
        print("[ ] unsync")
        assert ctx.guild is not None

        guild = discord.Object(id=ctx.guild.id)
        current_commands = self.bot.tree.get_commands(guild=guild)
        self.bot.tree.clear_commands(guild=guild)
        commands = await self.bot.tree.sync(guild=guild)
        for command in current_commands:
            self.bot.tree.add_command(command)
        await ctx.send(content=f"Synced {len(commands)} commands.")

    @discord.ext.commands.command()
    async def sync_global(self, ctx: discord.ext.commands.Context) -> None:
        print("[ ] sync_global")
        commands = await self.bot.tree.sync()
        await ctx.send(content=f"Synced {len(commands)} commands.")

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
