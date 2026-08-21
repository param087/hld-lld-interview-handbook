"""The event bus and the notification inbox.

Every service publishes here and none of them knows who listens. Handlers are
invoked outside every domain lock, so a slow notifier can never stall the graph.
"""

from __future__ import annotations

import threading
from typing import Protocol

from lld.linkedin.models import EventType, NetworkEvent


# --8<-- [start:bus]
class EventHandler(Protocol):
    def __call__(self, event: NetworkEvent) -> None: ...


class EventBus:
    """Topic fan-out. ``_lock`` guards the handler table, not the handlers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_type: dict[EventType, list[EventHandler]] = {}
        self._catch_all: list[EventHandler] = []

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        with self._lock:
            self._by_type.setdefault(event_type, []).append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        with self._lock:
            self._catch_all.append(handler)

    def publish(self, event: NetworkEvent) -> None:
        with self._lock:  # copy the list, then call outside the lock
            handlers = [*self._by_type.get(event.type, ()), *self._catch_all]
        for handler in handlers:
            handler(event)


class NotificationService:
    """The bell icon: one message list per recipient, and nothing about the domain."""

    TEMPLATES: dict[EventType, str] = {
        EventType.REQUEST_SENT: "{actor} wants to connect",
        EventType.REQUEST_ACCEPTED: "{actor} is now a connection",
        EventType.POST_PUBLISHED: "{actor} posted an update",
        EventType.POST_REACTED: "{actor} reacted to your post",
        EventType.MESSAGE_SENT: "{actor} sent you a message",
        EventType.APPLICATION_SUBMITTED: "{actor} applied for {subject}",
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inbox: dict[str, list[str]] = {}

    def __call__(self, event: NetworkEvent) -> None:
        if event.actor_id == event.recipient_id:
            return  # never notify someone about their own action
        template = self.TEMPLATES.get(event.type, "{actor} did something")
        text = template.format(actor=event.actor_id, subject=event.subject_id)
        if event.detail:
            text = f"{text}: {event.detail}"
        with self._lock:
            self._inbox.setdefault(event.recipient_id, []).append(text)

    def messages(self, member_id: str) -> list[str]:
        with self._lock:
            return list(self._inbox.get(member_id, ()))

    def unread(self, member_id: str) -> int:
        with self._lock:
            return len(self._inbox.get(member_id, ()))


# --8<-- [end:bus]
