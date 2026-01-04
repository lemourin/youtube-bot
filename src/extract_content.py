import subprocess
import sys
import os
import json
import dataclasses
import pathlib
import uuid
from types import TracebackType
from .util import (
    PlaybackOptions,
    FileStorageOptions,
    ytdl_time_range,
    audio_filter_graph,
)


@dataclasses.dataclass
class InlineAttachment:
    content_path: str
    ext: str

    def __enter__(self):
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ):
        os.remove(self.content_path)


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


class ExtractContentException(Exception):
    pass


def _transcode_h264_pass_1(input_v: str, pass_log_file: str, bitrate_v: int):
    with subprocess.Popen(
        args=[
            "ffmpeg",
            "-i",
            input_v,
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
            "threads=2:open-gop=0:keyint=120:min-keyint=60",
            "-an",
            "-f",
            "null",
            f"{"NUL" if os.name == "nt" else "/dev/null"}",
        ],
        stdout=sys.stdout,
    ) as proc:
        try:
            proc.wait(timeout=300)
            if proc.returncode != 0:
                raise ExtractContentException(
                    f"ffmpeg error {proc.returncode} on encode pass 1"
                )
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise ExtractContentException(f"ffmpeg encode pass 1 deadline exceeded")


def _transcode_h264_pass_2(
    input: list[str],
    output: str,
    pass_log_file: str,
    bitrate_v: int,
    bitrate_a: int,
    codec_a: str,
    graph: str | None,
):
    with subprocess.Popen(
        args=[
            "ffmpeg",
        ]
        + sum((["-i", path] for path in input), [])
        + [
            "-f",
            "mp4",
            "-movflags",
            "+faststart",
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
            "threads=2:open-gop=0:keyint=120:min-keyint=60",
            "-c:a",
            codec_a,
            "-b:a",
            f"{bitrate_a}",
        ]
        + (["-af", graph] if graph else [])
        + [output],
        stdout=sys.stdout,
    ) as proc:
        try:
            proc.wait(timeout=450)
            if proc.returncode != 0:
                raise ExtractContentException(
                    f"ffmpeg error {proc.returncode} on transcode"
                )
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise ExtractContentException(f"ffmpeg encode pass 2 deadline exceeded")


def _transcode_h265_pass_1(input_v: str, pass_log_file: str, bitrate_v: int):
    with subprocess.Popen(
        args=[
            "ffmpeg",
            "-i",
            input_v,
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
            f"stats={pass_log_file}:pass=1:frame-threads=2:pools=4:open-gop=0:keyint=120:min-keyint=60",
            "-an",
            "-f",
            "null",
            f"{"NUL" if os.name == "nt" else "/dev/null"}",
        ]
    ) as proc:
        try:
            proc.wait(timeout=300)
            if proc.returncode != 0:
                raise ExtractContentException(
                    f"ffmpeg error {proc.returncode} on encode pass 1"
                )
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise ExtractContentException(f"ffmpeg encode pass 1 deadline exceeded")


def _transcode_h265_pass_2(
    input: list[str],
    output: str,
    pass_log_file: str,
    bitrate_v: int,
    bitrate_a: int,
    codec_a: str,
    graph: str | None,
):
    with subprocess.Popen(
        args=["ffmpeg"]
        + sum((["-i", path] for path in input), [])
        + [
            "-f",
            "mp4",
            "-movflags",
            "+faststart",
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
            codec_a,
            "-b:a",
            f"{bitrate_a}",
            "-x265-params",
            f"stats={pass_log_file}:pass=2:frame-threads=2:pools=4:open-gop=0:keyint=120:min-keyint=60",
        ]
        + (["-af", graph] if graph else [])
        + [output]
    ) as proc:
        try:
            proc.wait(timeout=450)
            if proc.returncode != 0:
                raise ExtractContentException(
                    f"ffmpeg error {proc.returncode} on transcode"
                )
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise ExtractContentException(f"ffmpeg encode pass 2 deadline exceeded")


def _transcode_webp(input: str, output: str, bitrate_v: int):
    with subprocess.Popen(
        args=[
            "ffmpeg",
            "-i",
            input,
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
            output,
        ]
    ) as proc:
        try:
            proc.wait(timeout=600)
            if proc.returncode != 0:
                raise ExtractContentException(
                    f"ffmpeg error {proc.returncode} on transcode"
                )
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise ExtractContentException(f"ffmpeg webp transcode deadline exceeded")


def _transcode_audio(
    format: str,
    input_a: str,
    output: str,
    bitrate_a: int,
    codec_a: str,
    graph: str | None,
):
    with subprocess.Popen(
        args=[
            "ffmpeg",
            "-i",
            input_a,
            "-f",
            format,
        ]
        + (["-movflags", "+faststart"] if format == "mp4" else [])
        + [
            "-y",
            "-vn",
            "-c:a",
            codec_a,
            "-b:a",
            f"{bitrate_a}",
        ]
        + (["-af", graph] if graph else [])
        + [output]
    ) as proc:
        try:
            proc.wait(timeout=600)
            if proc.returncode != 0:
                raise ExtractContentException(
                    f"ffmpeg error {proc.returncode} on transcode"
                )
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise ExtractContentException(f"ffmpeg transcode deadline exceeded")


def _ffprobe_media(url: str):
    with subprocess.Popen(
        args=[
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            url,
        ],
        stdout=subprocess.PIPE,
    ) as proc:
        try:
            stdout, _ = proc.communicate(timeout=10)
            if proc.returncode != 0:
                raise ExtractContentException(
                    f"ffmpeg error {proc.returncode} on transcode"
                )
            data = json.loads(stdout)
            return data
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise ExtractContentException(f"ffprobe deadline exceeded")


def _supported_codecs():
    return ["h264", "h265", "hvc1", "avc1", "vp09"]


@dataclasses.dataclass
class FetchResult:
    title: str
    input_codec_v: str | None
    input_filename_v: str | None
    input_filename_a: str | None
    input_bitrate_v: int | None
    input_bitrate_a: int | None
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
            f"bv[{supported_vcodec}],ba",
            f"b[{supported_vcodec}]",
            "bv,ba",
            "b",
            f"bv[filesize<{max_video_size}][{supported_vcodec}]",
            f"bv[filesize_approx<{max_video_size}][{supported_vcodec}]",
            f"bv[filesize<{max_video_dl_size}][{supported_vcodec}]",
            f"bv[filesize_approx<{max_video_dl_size}][{supported_vcodec}]",
            f"bv[filesize<{max_video_dl_size}]",
            f"bv[filesize_approx<{max_video_dl_size}]",
            f"ba[filesize<{max_audio_dl_size}]",
            f"ba[filesize_approx<{max_audio_dl_size}]",
            "bv",
            "ba",
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
            f"{pathlib.Path(storage_options.tmp_file_path) / str(uuid)}-%(format_id)s.%(ext)s",
            url,
        ]
        + (["--download-sections", f"*{time_range}"] if time_range else []),
        stdout=subprocess.PIPE,
        text=True,
    ) as proc:
        try:
            stdout, _ = proc.communicate(timeout=120)
            if proc.returncode != 0:
                raise ExtractContentException(f"yt-dlp error {proc.returncode}")
            yt_dlp_info = json.loads(stdout)
            input_codec_v = None
            input_filename_v = None
            input_filename_a = None
            input_bitrate_v = None
            input_bitrate_a = None
            input_duration_seconds = None
            for path in os.scandir(storage_options.tmp_file_path):
                if str(uuid) not in path.path:
                    continue
                video = _ffprobe_media(path.path)
                video_format = video["format"]
                for stream in video["streams"]:
                    if input_filename_v is None and stream["codec_type"] == "video":
                        input_filename_v = path.path
                        input_codec_v = stream["codec_tag_string"]
                        input_bitrate_v = (
                            int(stream["bit_rate"])
                            if "bit_rate" in stream
                            else int(video_format["bit_rate"])
                        )
                        input_duration_seconds = (
                            int(float(stream["duration"]))
                            if "duration" in stream
                            else int(float(video_format["duration"]))
                        )
                    if input_filename_a is None and stream["codec_type"] == "audio":
                        input_filename_a = path.path
                        input_bitrate_a = (
                            int(stream["bit_rate"])
                            if "bit_rate" in stream
                            else int(video_format["bit_rate"])
                        )
                        input_duration_seconds = (
                            int(float(stream["duration"]))
                            if "duration" in stream
                            else int(float(video_format["duration"]))
                        )

            if input_filename_v == input_filename_a:
                input_filename_a = None

            if input_duration_seconds is None:
                raise ExtractContentException("Duration unknown!")

            return FetchResult(
                title=yt_dlp_info.get("title") or "No title.",
                input_codec_v=input_codec_v,
                input_filename_v=input_filename_v,
                input_filename_a=input_filename_a,
                input_bitrate_v=input_bitrate_v,
                input_bitrate_a=input_bitrate_a or 0,
                duration_seconds=input_duration_seconds,
            )

        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise ExtractContentException(f"yt-dlp fetch deadline exceeded")


def _two_pass_transcode(
    codec_v: str,
    codec_a: str,
    options: PlaybackOptions,
    pass_log_file: str,
    bitrate_v: int,
    bitrate_a: int,
    input_v: str,
    input_a: str | None,
    output: str,
):
    transcode_pass_1 = (
        _transcode_h265_pass_1 if (codec_v == "h265") else _transcode_h264_pass_1
    )
    transcode_pass_2 = (
        _transcode_h265_pass_2 if (codec_v == "h265") else _transcode_h264_pass_2
    )
    transcode_pass_1(input_v, pass_log_file, bitrate_v)
    transcode_pass_2(
        input=[input_v] + ([input_a] if input_a is not None else []),
        output=output,
        pass_log_file=pass_log_file,
        bitrate_v=bitrate_v,
        bitrate_a=bitrate_a,
        codec_a=codec_a,
        graph=audio_filter_graph(options),
    )


def _remux_video(
    format: str,
    input: list[str],
    output_filepath: str,
    bitrate_a: int,
    audio_codec: str,
    graph: str | None,
):
    with subprocess.Popen(
        args=["ffmpeg"]
        + sum((["-i", path] for path in input), [])
        + [
            "-f",
            format,
        ]
        + (["-movflags", "+faststart"] if format == "mp4" else [])
        + [
            "-y",
            "-c:v",
            "copy",
            "-c:a",
            audio_codec,
            "-b:a",
            f"{bitrate_a}",
        ]
        + (["-af", graph] if graph else [])
        + [output_filepath],
    ) as proc:
        try:
            proc.wait(timeout=600)
            if proc.returncode != 0:
                raise ExtractContentException(
                    f"ffmpeg error {proc.returncode} on remux"
                )
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
            raise ExtractContentException(f"ffmpeg remux deadline exceeded")


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
    input_codec_v: str | None,
    dest_codec_v: str,
    input_bitrate_v: int | None,
    input_bitrate_a: int | None,
    bitrate_v: int,
    bitrate_a: int,
    duration_seconds: int,
    filesize_limit: int,
    input_v: str | None,
    input_a: str | None,
):
    def _output_filepath(format: str):
        if storage_options.storage_path:
            return str(
                pathlib.Path(storage_options.storage_path) / f"{input_uuid}.{format}"
            )
        else:
            return str(
                pathlib.Path(storage_options.tmp_file_path) / f"{input_uuid}.{format}"
            )

    output_filepath = None
    try:
        if extension == "webp":
            if input_v is None:
                raise ExtractContentException("Can't make a GIF with no video.")
            output_filepath = _output_filepath("webm")
            _transcode_webp(
                input=input_v,
                output=output_filepath,
                bitrate_v=bitrate_v,
            )
            return output_filepath
        if input_codec_v is None or input_bitrate_v is None or input_v is None:
            if input_a is None:
                raise ExtractContentException("No media file to convert.")
            output_filepath = _output_filepath("m4a")
            _transcode_audio(
                format="mp4",
                input_a=input_a,
                output=output_filepath,
                bitrate_a=bitrate_a,
                codec_a=storage_options.attachment.mp4_codec_a,
                graph=options.filter_graph,
            )
            return output_filepath
        if _needs_video_transcode(
            vcodec=input_codec_v,
            input_bitrate_v=input_bitrate_v,
            input_bitrate_a=bitrate_a,
            duration_seconds=duration_seconds,
            filesize_limit=filesize_limit,
        ):
            output_filepath = _output_filepath("mp4")
            _two_pass_transcode(
                codec_v=dest_codec_v,
                codec_a=storage_options.attachment.mp4_codec_a,
                options=options,
                pass_log_file=str(
                    pathlib.Path(storage_options.tmp_file_path) / f"{input_uuid}.log"
                ),
                bitrate_v=bitrate_v,
                bitrate_a=bitrate_a,
                input_v=input_v,
                input_a=input_a,
                output=output_filepath,
            )
            return output_filepath
        else:
            format = "webm" if input_codec_v.startswith("vp09") else "mp4"
            output_filepath = _output_filepath(format)
            _remux_video(
                format=format,
                input=[input_v] + ([input_a] if input_a else []),
                output_filepath=output_filepath,
                bitrate_a=bitrate_a,
                audio_codec=(
                    storage_options.attachment.webm_codec_a
                    if format == "webm"
                    else storage_options.attachment.mp4_codec_a
                ),
                graph=options.filter_graph,
            )
            return output_filepath
    except:
        if output_filepath and os.path.exists(output_filepath):
            os.remove(output_filepath)
        raise


def extract_content(
    url: str,
    options: PlaybackOptions,
    filesize_limit: int,
    storage_options: FileStorageOptions,
    extension: str = "mp4",
    video_codec: str = "h265",
) -> Attachment:
    input_uuid = uuid.uuid4()
    output_filepath = None
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
            raise ExtractContentException("File too big!")

        output_filepath = _convert_video(
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
            input_v=input_filename_v,
            input_a=input_filename_a,
        )

        if storage_options.url_path:
            url = f"{storage_options.url_path}{os.path.basename(output_filepath)}"
            return Attachment(title=title, url=url)
        else:
            return Attachment(
                title=title,
                inline_attachment=InlineAttachment(content_path=output_filepath, ext=extension),  # type: ignore
            )
    except:
        if output_filepath and os.path.exists(output_filepath):
            os.remove(output_filepath)
        raise
    finally:
        for path in os.scandir(storage_options.tmp_file_path):
            if str(input_uuid) in path.path and path.path != output_filepath:
                os.remove(path)
