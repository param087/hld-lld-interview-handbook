"""Chat routing: session registry, cross-server pub/sub, per-conversation sequencing and acks.

The crux of the chat-system design in one module:

* ``SessionRegistry`` answers "which chat server holds this device's WebSocket?" (a Redis hash
  ``sessions:{user_id}`` plus a per-server liveness key with a TTL in production).
* ``ServerBus`` is the cross-server pub/sub: every chat server subscribes to its own channel and
  the router publishes to the channel of whichever server owns the recipient's socket.
* ``ChatService`` assigns one monotonically increasing ``seq`` per conversation, stores the
  message, routes it to every online device (including the sender's other devices), hands
  offline recipients to the push service, and tracks per-recipient delivery state
  ``sent -> delivered -> read``.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from common import Clock, NotFoundError, SystemClock, ValidationError


# --8<-- [start:models]
class DeliveryState(StrEnum):
    """Per-recipient state of a message; transitions only move forward."""

    SENT = "sent"  # sequenced and durably stored by the server
    DELIVERED = "delivered"  # a device of the recipient acked receipt
    READ = "read"  # the recipient opened the conversation

    @property
    def rank(self) -> int:
        return list(DeliveryState).index(self)


@dataclass(frozen=True, slots=True)
class Message:
    conversation_id: str
    seq: int  # per-conversation sequence number: total order and gap detection
    sender_id: str
    client_msg_id: str  # idempotency key chosen by the sending device
    body: str
    sent_at: float

    @property
    def message_id(self) -> str:
        return f"{self.conversation_id}:{self.seq}"


@dataclass(frozen=True, slots=True)
class Session:
    user_id: str
    device_id: str
    server_id: str


@dataclass(frozen=True, slots=True)
class Envelope:
    """What travels over the bus: one message addressed to one socket."""

    session: Session
    message: Message


# --8<-- [end:models]


# --8<-- [start:registry]
class SessionRegistry:
    """Maps (user, device) to the chat server that holds its WebSocket.

    Production shape: ``HSET sessions:{user_id} {device_id} {server_id}`` written by the server
    that accepted the socket, plus one liveness key per server (``SET server:{id} 1 EX ttl``)
    refreshed every few seconds. Heartbeats are per server, not per socket, so 10M sockets cost
    a few hundred Redis writes per second instead of hundreds of thousands. A session whose
    server stopped heartbeating is treated as gone and dropped on the next read. ``_lock``
    protects ``_sessions`` and ``_server_seen``.
    """

    def __init__(self, clock: Clock | None = None, ttl_s: float = 15.0) -> None:
        self._clock = clock or SystemClock()
        self._ttl = ttl_s
        self._sessions: dict[str, dict[str, Session]] = defaultdict(dict)  # user -> device -> session
        self._server_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def heartbeat(self, server_id: str) -> None:
        with self._lock:
            self._server_seen[server_id] = self._clock.now()

    def connect(self, user_id: str, device_id: str, server_id: str) -> Session:
        session = Session(user_id, device_id, server_id)
        with self._lock:
            if server_id not in self._server_seen:
                raise ValidationError(f"server {server_id} never sent a heartbeat")
            self._sessions[user_id][device_id] = session
        return session

    def disconnect(self, user_id: str, device_id: str) -> None:
        with self._lock:
            self._sessions[user_id].pop(device_id, None)

    def sessions(self, user_id: str) -> list[Session]:
        """Live sessions only: entries owned by a dead server are dropped on read (lazy TTL)."""
        now = self._clock.now()
        with self._lock:
            live: list[Session] = []
            for device_id, session in list(self._sessions[user_id].items()):
                if now - self._server_seen.get(session.server_id, float("-inf")) > self._ttl:
                    del self._sessions[user_id][device_id]
                else:
                    live.append(session)
            return live

    def is_online(self, user_id: str) -> bool:
        return bool(self.sessions(user_id))


# --8<-- [end:registry]


# --8<-- [start:bus]
class ServerBus:
    """Cross-server pub/sub: one channel per chat server (Redis pub/sub in production)."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[Envelope], None]] = {}
        self._lock = threading.Lock()

    def subscribe(self, server_id: str, handler: Callable[[Envelope], None]) -> None:
        with self._lock:
            self._handlers[server_id] = handler

    def unsubscribe(self, server_id: str) -> None:
        with self._lock:
            self._handlers.pop(server_id, None)

    def publish(self, server_id: str, envelope: Envelope) -> bool:
        """False when nobody listens on the channel: the registry entry pointed at a dead server."""
        with self._lock:
            handler = self._handlers.get(server_id)
        if handler is None:
            return False
        handler(envelope)
        return True


class ChatServer:
    """One WebSocket server: it owns the sockets of the devices connected to it, nothing else."""

    def __init__(self, server_id: str, registry: SessionRegistry, bus: ServerBus) -> None:
        self.server_id = server_id
        self._registry = registry
        self._outboxes: dict[tuple[str, str], list[Message]] = {}  # socket -> frames written
        self._lock = threading.Lock()
        registry.heartbeat(server_id)
        bus.subscribe(server_id, self.deliver)

    def heartbeat(self) -> None:
        self._registry.heartbeat(self.server_id)

    def connect(self, user_id: str, device_id: str) -> Session:
        with self._lock:
            self._outboxes[(user_id, device_id)] = []
        return self._registry.connect(user_id, device_id, self.server_id)

    def disconnect(self, user_id: str, device_id: str) -> None:
        with self._lock:
            self._outboxes.pop((user_id, device_id), None)
        self._registry.disconnect(user_id, device_id)

    def deliver(self, envelope: Envelope) -> None:
        key = (envelope.session.user_id, envelope.session.device_id)
        with self._lock:
            outbox = self._outboxes.get(key)
            if outbox is not None:
                outbox.append(envelope.message)
            # else the socket closed between lookup and delivery; the device catches up via sync()

    def outbox(self, user_id: str, device_id: str) -> list[Message]:
        with self._lock:
            return list(self._outboxes.get((user_id, device_id), []))


# --8<-- [end:bus]


# --8<-- [start:service]
class ChatService:
    """Sequencer, message store, router and receipts in one class so the flow is readable.

    In production these are separate parts: a per-conversation sequencer (the owner of the
    conversation's partition), a wide-column message table keyed by ``conversation_id`` and
    clustered by ``seq``, stateless routers and a receipts table. ``_lock`` protects the
    counters, the store, the idempotency map, the receipts and the push list; routing runs
    outside it so a slow socket never blocks sequencing.
    """

    def __init__(self, registry: SessionRegistry, bus: ServerBus, clock: Clock | None = None) -> None:
        self._registry = registry
        self._bus = bus
        self._clock = clock or SystemClock()
        self._members: dict[str, frozenset[str]] = {}
        self._next_seq: dict[str, int] = defaultdict(lambda: 1)
        self._store: dict[str, list[Message]] = defaultdict(list)  # conversation_id -> by seq
        self._dedup: dict[tuple[str, str], Message] = {}  # (sender_id, client_msg_id) -> message
        self._receipts: dict[str, dict[str, DeliveryState]] = {}  # message_id -> recipient -> state
        self._pushes: list[tuple[str, str]] = []  # (user_id, message_id) handed to push
        self._lock = threading.Lock()

    def create_conversation(self, conversation_id: str, members: set[str]) -> None:
        if len(members) < 2:
            raise ValidationError("a conversation needs at least two members")
        with self._lock:
            self._members[conversation_id] = frozenset(members)

    # -- write path ----------------------------------------------------------------
    def send(self, sender: Session, conversation_id: str, client_msg_id: str, body: str) -> Message:
        if not body.strip():
            raise ValidationError("empty message")
        with self._lock:
            members = self._members.get(conversation_id)
            if members is None or sender.user_id not in members:
                raise NotFoundError(f"{sender.user_id} is not a member of {conversation_id}")
            key = (sender.user_id, client_msg_id)
            if key in self._dedup:
                return self._dedup[key]  # client retry after a lost ack: same seq, no duplicate
            seq = self._next_seq[conversation_id]
            self._next_seq[conversation_id] = seq + 1
            message = Message(conversation_id, seq, sender.user_id, client_msg_id, body, self._clock.now())
            self._store[conversation_id].append(message)
            self._dedup[key] = message
            self._receipts[message.message_id] = {
                m: DeliveryState.SENT for m in members if m != sender.user_id
            }
        for member in members:
            self._route(member, message, exclude=sender)
        return message

    def _route(self, user_id: str, message: Message, exclude: Session) -> None:
        delivered = False
        for session in self._registry.sessions(user_id):
            if session == exclude:
                continue  # the sending device already has the message
            if self._bus.publish(session.server_id, Envelope(session, message)):
                delivered = True
            else:
                self._registry.disconnect(session.user_id, session.device_id)  # dead server
        if not delivered and user_id != exclude.user_id:
            with self._lock:
                self._pushes.append((user_id, message.message_id))

    # -- acks ------------------------------------------------------------------------
    def ack_delivered(self, user_id: str, message_id: str) -> DeliveryState:
        return self._advance(user_id, message_id, DeliveryState.DELIVERED)

    def mark_read(self, user_id: str, conversation_id: str, up_to_seq: int) -> int:
        """Read receipts are a cursor: one ack marks everything up to ``up_to_seq`` as read."""
        with self._lock:
            pending: list[str] = []
            for m in self._store[conversation_id]:
                if m.seq > up_to_seq:
                    break
                state = self._receipts[m.message_id].get(user_id)
                if state is not None and state is not DeliveryState.READ:
                    pending.append(m.message_id)
        for message_id in pending:
            self._advance(user_id, message_id, DeliveryState.READ)
        return len(pending)

    def _advance(self, user_id: str, message_id: str, target: DeliveryState) -> DeliveryState:
        with self._lock:
            receipts = self._receipts.get(message_id)
            if receipts is None or user_id not in receipts:
                raise NotFoundError(f"{user_id} is not a recipient of {message_id}")
            if target.rank > receipts[user_id].rank:
                receipts[user_id] = target  # forward only: a late delivered ack never undoes read
            return receipts[user_id]

    def status(self, message_id: str) -> DeliveryState:
        """What the sender sees: the weakest state across recipients (all must read for read)."""
        with self._lock:
            receipts = self._receipts.get(message_id)
            if receipts is None:
                raise NotFoundError(message_id)
            return min(receipts.values(), key=lambda s: s.rank)

    # -- read path -----------------------------------------------------------------
    def sync(self, user_id: str, conversation_id: str, after_seq: int, limit: int = 100) -> list[Message]:
        """Catch-up for a reconnecting or new device: everything after the last seq it holds."""
        with self._lock:
            members = self._members.get(conversation_id)
            if members is None or user_id not in members:
                raise NotFoundError(f"{user_id} is not a member of {conversation_id}")
            return [m for m in self._store[conversation_id] if m.seq > after_seq][:limit]

    def pushes(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._pushes)


# --8<-- [end:service]


def main() -> None:
    from common import FakeClock

    clock = FakeClock(start=1_700_000_000)
    registry = SessionRegistry(clock, ttl_s=15)
    bus = ServerBus()
    ws1, ws2 = ChatServer("ws-1", registry, bus), ChatServer("ws-2", registry, bus)
    ann_phone = ws1.connect("ann", "ann-phone")
    ws2.connect("ann", "ann-laptop")
    bob_phone = ws2.connect("bob", "bob-phone")  # cat is offline
    chat = ChatService(registry, bus, clock)
    chat.create_conversation("trio", {"ann", "bob", "cat"})

    m1 = chat.send(ann_phone, "trio", client_msg_id="c1", body="lunch?")
    dup = chat.send(ann_phone, "trio", client_msg_id="c1", body="lunch?")  # retry after lost ack
    print(f"{m1.message_id} sent from ann-phone; retry returned seq {dup.seq} (idempotent)")
    print("ann-laptop outbox:", [m.body for m in ws2.outbox("ann", "ann-laptop")], "(multi-device)")
    print("bob-phone outbox: ", [m.body for m in ws2.outbox("bob", "bob-phone")], "(via ws-2 channel)")
    print("push notifications:", chat.pushes(), "| cat online:", registry.is_online("cat"))
    print("status:", chat.status(m1.message_id))
    chat.ack_delivered("bob", m1.message_id)
    print("after bob's delivered ack:", chat.status(m1.message_id), "(cat has not received it)")
    clock.advance(5)
    ws1.heartbeat()
    ws2.heartbeat()
    ws1.connect("cat", "cat-phone")
    missed = chat.sync("cat", "trio", after_seq=0)
    print("cat connects, sync(after_seq=0) ->", [(m.seq, m.body) for m in missed])
    chat.ack_delivered("cat", m1.message_id)
    print("after cat's delivered ack:", chat.status(m1.message_id))
    chat.mark_read("cat", "trio", up_to_seq=1)
    chat.mark_read("bob", "trio", up_to_seq=1)
    print("after both read:", chat.status(m1.message_id))
    m2 = chat.send(bob_phone, "trio", "b1", "yes, 12:30")
    print(f"{m2.message_id}: the order inside a conversation is a counter, never a clock")
    bus.unsubscribe("ws-2")  # ws-2 crashes without disconnecting its sockets
    chat.send(ann_phone, "trio", "c2", "see you there")
    print("ws-2 gone: bob's session dropped ->", chat.pushes()[-1], "| bob online:", registry.is_online("bob"))


if __name__ == "__main__":
    main()
