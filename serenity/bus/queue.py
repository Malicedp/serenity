"""Async message queue for decoupled channel-agent communication."""

import asyncio

from loguru import logger

from serenity.bus.events import InboundMessage, OutboundMessage

# Bounded queue sizes — prevents OOM under Telegram floods or a slow agent.
# At 500 inbound messages the oldest ones are dropped with a warning.
_INBOUND_MAXSIZE  = 500
_OUTBOUND_MAXSIZE = 500


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.

    Both queues are bounded to prevent unbounded memory growth under load.
    Overflow messages are dropped with a warning log.
    """

    def __init__(self) -> None:
        self.inbound:  asyncio.Queue[InboundMessage]  = asyncio.Queue(maxsize=_INBOUND_MAXSIZE)
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=_OUTBOUND_MAXSIZE)

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent.

        Drops the message (with a warning) if the queue is full rather than
        blocking the caller indefinitely.
        """
        try:
            self.inbound.put_nowait(msg)
        except asyncio.QueueFull:
            logger.warning(
                "MessageBus: inbound queue full ({} msgs) — dropping message from {}",
                _INBOUND_MAXSIZE, getattr(msg, "channel", "?"),
            )

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        msg = await self.inbound.get()
        self.inbound.task_done()
        return msg

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        try:
            self.outbound.put_nowait(msg)
        except asyncio.QueueFull:
            logger.warning(
                "MessageBus: outbound queue full ({} msgs) — dropping response",
                _OUTBOUND_MAXSIZE,
            )

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        msg = await self.outbound.get()
        self.outbound.task_done()
        return msg

    async def drain(self) -> None:
        """Wait until both queues are fully processed (for clean shutdown)."""
        await self.inbound.join()
        await self.outbound.join()

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()
