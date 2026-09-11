"""Reusable Discord UI components.

Not a cog — imported explicitly by the cogs that need it.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import discord

_log = logging.getLogger(__name__)

# Long enough to read a preview and decide, short enough that a forgotten
# prompt does not sit around holding a live view. Well inside the 15 minute
# lifetime of an interaction token.
DEFAULT_TIMEOUT = 120.0


class ConfirmView(discord.ui.View):
    """A Confirm/Cancel prompt attached to one user's ephemeral message.

    The buttons only record the decision; the caller awaits :meth:`wait` and
    does the work, so the irreversible action stays in the cog rather than
    being buried in a callback::

        view = ConfirmView(ctx.interaction, user_id=ctx.author.id)
        await ctx.respond("Sure?", view=view, ephemeral=True)
        await view.wait()

        if view.choice is None:
            return                      # timed out; on_timeout said so already
        if not view.choice:
            await view.finish("Cancelled.")
            return

        await view.finish("Done.")
    """

    def __init__(
        self,
        origin: discord.Interaction,
        *,
        user_id: int,
        confirm_label: str = "Confirm",
        confirm_style: discord.ButtonStyle = discord.ButtonStyle.danger,
        timeout_message: str = "Timed out — nothing happened.",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)

        # The interaction whose *original response* carries this view. Editing
        # through it is the only way to reach an ephemeral message later.
        self._origin = origin
        self._user_id = user_id
        self._timeout_message = timeout_message

        #: True if confirmed, False if cancelled, None if it timed out.
        self.choice: bool | None = None

        self.add_item(self._button(confirm_label, confirm_style, choice=True))
        self.add_item(self._button("Cancel", discord.ButtonStyle.secondary, choice=False))

    def _button(
        self,
        label: str,
        style: discord.ButtonStyle,
        *,
        choice: bool,
    ) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=style)
        button.callback = self._record(choice)
        return button

    def _record(self, choice: bool) -> Callable[[discord.Interaction], Coroutine[Any, Any, None]]:
        """Build the callback that stores ``choice`` and releases :meth:`wait`."""

        async def callback(interaction: discord.Interaction) -> None:
            self.choice = choice
            # Grey the buttons out immediately: the work may take a moment and
            # this also acknowledges the click inside Discord's 3 second window.
            self.disable_all_items()
            await interaction.response.edit_message(view=self)
            self.stop()

        return callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Refuse clicks from anyone but the user who opened the prompt."""
        if interaction.user is not None and interaction.user.id != self._user_id:
            await interaction.response.send_message("That prompt is not yours.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        """Say the prompt expired, rather than leaving live buttons behind."""
        self.disable_all_items()
        with contextlib.suppress(discord.HTTPException):
            await self.finish(self._timeout_message, view=self)

    async def finish(
        self,
        content: str,
        *,
        view: discord.ui.View | None = None,
    ) -> None:
        """Replace the prompt with its outcome, dropping the buttons."""
        await self._origin.edit_original_response(
            content=content,
            # Clear the preview embed, if there was one.
            embed=None,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
