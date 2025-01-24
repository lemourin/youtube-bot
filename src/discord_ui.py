from typing import override, Callable, Awaitable
import discord


class View(discord.ui.View):
    def __init__(self, on_timeout: Callable[[], Awaitable[None]], **kwargs):
        super().__init__(**kwargs)
        self._on_timeout = on_timeout

    async def on_timeout(self) -> None:
        await self._on_timeout()


class SelectView(discord.ui.Select):
    def __init__(
        self, callback: Callable[[discord.Interaction], Awaitable[None]], **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self._callback = callback

    def selected_value(self) -> str:
        return self.values[0]

    @override
    async def callback(self, interaction: discord.Interaction) -> None:
        return await self._callback(interaction)


class ButtonView(discord.ui.Button):
    def __init__(
        self, callback: Callable[[discord.Interaction], Awaitable[None]], **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self._callback = callback

    @override
    async def callback(self, interaction: discord.Interaction) -> None:
        return await self._callback(interaction)
