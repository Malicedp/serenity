# Copyright © 2026 Daniel T Niamke. All rights reserved.
"""call_user tool — Serenity proactively contacts the user.

Sends a text message via the configured channel (Telegram, etc.).
Telegram's own notification system makes the phone buzz like a call.

Wired in AgentLoop the same way as MessageTool:
  loop.tools.register(CallUserTool(send_callback=bus.publish_outbound))
  # context set per-turn via tool.set_context(channel, chat_id)
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from loguru import logger

from serenity.agent.tools.base import Tool, tool_parameters
from serenity.agent.tools.schema import StringSchema, tool_parameters_schema
from serenity.bus.events import OutboundMessage


@tool_parameters(
    tool_parameters_schema(
        message=StringSchema(
            "What Serenity should say / the alert message to deliver to the user."
        ),
        required=["message"],
    )
)
class CallUserTool(Tool):
    """Proactively contact the user — send them a message without being asked first.

    Use this when Serenity needs to alert or reach out unsolicited.
    The message lands in Telegram and buzzes the phone like a notification.

    Trigger phrases:
      "call me if …", "let me know …", "reach out to me …",
      "alert me …", "send me a message …", "call me when …",
      "ping me …", "notify me …", "message me if …"
    """

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
    ) -> None:
        self._send_callback = send_callback
        self._default_channel = default_channel
        self._default_chat_id = default_chat_id

    def set_context(self, channel: str, chat_id: str, *_: Any) -> None:
        self._default_channel = channel
        self._default_chat_id = chat_id

    def set_send_callback(
        self, callback: Callable[[OutboundMessage], Awaitable[None]]
    ) -> None:
        self._send_callback = callback

    @property
    def name(self) -> str:
        return "call_user"

    @property
    def description(self) -> str:
        return (
            "Proactively send the user a message — no prompting needed. "
            "Use when Serenity wants to alert or reach out on her own initiative. "
            "Trigger phrases: 'call me if', 'let me know', 'alert me', "
            "'send me a message', 'ping me', 'notify me', 'message me if', "
            "'call me when', 'reach out to me'."
        )

    async def execute(
        self,
        message: str,
        **kwargs: Any,
    ) -> str:
        channel = self._default_channel
        chat_id = self._default_chat_id

        if not channel or not chat_id:
            return "Error: no target channel configured — cannot contact user."
        if not self._send_callback:
            return "Error: message bus not wired — cannot contact user."

        try:
            msg = OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=message,
            )
            await self._send_callback(msg)
            logger.info("call_user: message sent to {}:{}", channel, chat_id)
        except Exception as e:
            logger.error("call_user: send failed — {}", e)
            return f"Failed to reach user: {e}"

        return f"User contacted: «{message[:80]}»"
