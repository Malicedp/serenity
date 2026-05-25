"""Message bus module for decoupled channel-agent communication."""

from serenity.bus.events import InboundMessage, OutboundMessage
from serenity.bus.queue import MessageBus

__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
