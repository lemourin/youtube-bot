#!/usr/bin/env python

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
import logging
import aiohttp
import aiohttp.web
import discord
import discord.ext.commands
from dotenv import load_dotenv
import googleapiclient.discovery  # type: ignore
from jellyfin_apiclient_python import JellyfinClient  # type: ignore
from src.discord_cog import DiscordCog, JellyfinLibraryClient
from src.util import FileStorageOptions, AttachmentOptions

load_dotenv(dotenv_path=os.environ.get("ENV_FILE"))

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_BOT_COMMAND_PREFIX = os.environ.get("DISCORD_BOT_COMMAND_PREFIX", "!")
DISCORD_ADMIN_ID = int(os.environ["DISCORD_ADMIN_ID"])
HEALTHCHECK_ADDRESS = os.environ.get("HEALTHCHECK_ADDRESS")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY")
JELLYFIN_APP_NAME = os.environ.get("JELLYFIN_APP_NAME")
JELLYFIN_APP_VERSION = os.environ.get("JELLYFIN_APP_VERSION")
JELLYFIN_ADDRESS = os.environ.get("JELLYFIN_ADDRESS")
JELLYFIN_USER_ID = os.environ.get("JELLYFIN_USER_ID")
JELLYFIN_DEVICE_ID = os.environ.get("JELLYFIN_DEVICE_ID")
JELLYFIN_LIBRARY_ID = os.environ.get("JELLYFIN_LIBRARY_ID")
JELLYFIN_USERNAME = os.environ.get("JELLYFIN_USERNAME")
JELLYFIN_PASSWORD = os.environ.get("JELLYFIN_PASSWORD")
FILE_STORAGE_PATH = os.environ.get("FILE_STORAGE_PATH")
FILE_URL_PATH = os.environ.get("FILE_URL_PATH")
TMP_FILE_PATH = os.environ.get("TMP_FILE_PATH", "./tmp/")
ATTACHMENT_MAX_SIZE = os.environ.get("ATTACHMENT_MAX_SIZE")
ATTACHMENT_MAX_VIDEO_DL_SIZE = int(
    os.environ.get("ATTACHMENT_MAX_VIDEO_DL_SIZE", str(128 * 1024 * 1924))
)
ATTACHMENT_MAX_AUDIO_DL_SIZE = int(
    os.environ.get("ATTACHMENT_MAX_AUDIO_DL_SIZE", str(64 * 1024 * 1024))
)
ATTACHMENT_AUDIO_BITRATE = int(os.environ.get("ATTACHMENT_AUDIO_BITRATE", 64_000))
ATTACHMENT_MIN_VIDEO_BITRATE = int(
    os.environ.get("ATTACHMENT_MIN_VIDEO_BITRATE", 64_000)
)
ATTACHMENT_MAX_VIDEO_BITRATE = int(
    os.environ.get("ATTACHMENT_MAX_VIDEO_BITRATE", 4_096_000)
)
ATTACHMENT_MP4_AUDIO_CODEC = os.environ.get("ATTACHMENT_MP4_AUDIO_CODEC", "aac")
ATTACHMENT_WEBM_AUDIO_CODEC = os.environ.get("ATTACHMENT_WEBM_AUDIO_CODEC", "libopus")


async def healthcheck(http: aiohttp.ClientSession) -> None:
    if not HEALTHCHECK_ADDRESS or len(HEALTHCHECK_ADDRESS) == 0:
        return
    while True:
        try:
            async with http.get(HEALTHCHECK_ADDRESS) as response:
                await response.text()
        except aiohttp.web.HTTPException as e:
            print(f"[ ] health check error {e}")
        await asyncio.sleep(60)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    intents = discord.Intents.default()
    intents.message_content = True

    bot = discord.ext.commands.Bot(
        command_prefix=discord.ext.commands.when_mentioned_or(
            DISCORD_BOT_COMMAND_PREFIX
        ),
        intents=intents,
    )

    jellyfin_client: JellyfinLibraryClient | None = None
    if JELLYFIN_ADDRESS and JELLYFIN_LIBRARY_ID:
        client = JellyfinClient()
        client.config.data["auth.ssl"] = True
        client.config.app(
            name=JELLYFIN_APP_NAME,
            version=JELLYFIN_APP_VERSION,
            device_name=JELLYFIN_APP_NAME,
            device_id=JELLYFIN_DEVICE_ID,
        )
        client.auth.connect_to_address(JELLYFIN_ADDRESS)
        if not client.auth.login(
            JELLYFIN_ADDRESS, JELLYFIN_USERNAME, JELLYFIN_PASSWORD
        ):
            raise RuntimeError("Jellyfin login failed.")
        jellyfin_client = JellyfinLibraryClient(
            client, JELLYFIN_LIBRARY_ID, JELLYFIN_ADDRESS
        )

    youtube_client = None
    if YOUTUBE_API_KEY:
        youtube_client = googleapiclient.discovery.build(
            "youtube",
            "v3",
            developerKey=YOUTUBE_API_KEY,
        )

    if os.path.isdir(TMP_FILE_PATH):
        for path in os.scandir(TMP_FILE_PATH):
            os.remove(path)
    else:
        os.mkdir(TMP_FILE_PATH)

    async with bot, aiohttp.ClientSession() as http_session:
        with ThreadPoolExecutor(max_workers=32) as executor:
            await bot.add_cog(
                DiscordCog(
                    bot,
                    executor,
                    http=http_session,
                    discord_admin_id=DISCORD_ADMIN_ID,
                    jellyfin_client=jellyfin_client,
                    youtube_client=youtube_client,
                    file_storage_options=FileStorageOptions(
                        storage_path=FILE_STORAGE_PATH,
                        url_path=FILE_URL_PATH,
                        tmp_file_path=TMP_FILE_PATH,
                        attachment=AttachmentOptions(
                            max_size=(
                                int(ATTACHMENT_MAX_SIZE)
                                if ATTACHMENT_MAX_SIZE
                                else None
                            ),
                            max_video_dl_size=ATTACHMENT_MAX_VIDEO_DL_SIZE,
                            max_audio_dl_size=ATTACHMENT_MAX_AUDIO_DL_SIZE,
                            audio_bitrate=ATTACHMENT_AUDIO_BITRATE,
                            min_video_bitrate=ATTACHMENT_MIN_VIDEO_BITRATE,
                            max_video_bitrate=ATTACHMENT_MAX_VIDEO_BITRATE,
                            mp4_codec_a=ATTACHMENT_MP4_AUDIO_CODEC,
                            webm_codec_a=ATTACHMENT_WEBM_AUDIO_CODEC,
                        ),
                    ),
                )
            )
            await asyncio.gather(
                bot.start(DISCORD_BOT_TOKEN), healthcheck(http_session)
            )


if __name__ == "__main__":
    asyncio.run(main())
