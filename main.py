#!/usr/bin/env python

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
import logging
import aiohttp
import discord
import discord.ext.commands
from dotenv import load_dotenv
import googleapiclient.discovery
from jellyfin_apiclient_python import JellyfinClient
from src.discord_cog import DiscordCog, JellyfinLibraryClient

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
JELLYFIN_LIBRARY_ID = os.environ.get("JELLYFIN_LIBRARY_ID")


async def healthcheck(http: aiohttp.ClientSession) -> None:
    if not HEALTHCHECK_ADDRESS or len(HEALTHCHECK_ADDRESS) == 0:
        return
    while True:
        try:
            async with http.get(HEALTHCHECK_ADDRESS) as response:
                await response.text()
        except aiohttp.web_exceptions.HTTPException as e:
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
        client.config.data["app.name"] = JELLYFIN_APP_NAME
        client.config.data["app.version"] = JELLYFIN_APP_VERSION
        client.config.data["auth.ssl"] = True
        client.authenticate(
            {
                "Servers": [
                    {
                        "AccessToken": JELLYFIN_API_KEY,
                        "address": JELLYFIN_ADDRESS,
                        "UserId": JELLYFIN_USER_ID,
                    }
                ]
            },
            discover=False,
        )
        jellyfin_client = JellyfinLibraryClient(client, JELLYFIN_LIBRARY_ID)

    youtube_client = None
    if YOUTUBE_API_KEY:
        youtube_client = googleapiclient.discovery.build(
            "youtube",
            "v3",
            developerKey=YOUTUBE_API_KEY,
        )

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
                )
            )
            await asyncio.gather(
                bot.start(DISCORD_BOT_TOKEN), healthcheck(http_session)
            )


if __name__ == "__main__":
    asyncio.run(main())
