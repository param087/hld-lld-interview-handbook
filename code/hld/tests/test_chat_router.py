from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, NotFoundError, ValidationError
from hld.chat_router import ChatServer, ChatService, DeliveryState, ServerBus, SessionRegistry


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000.0)


@pytest.fixture
def world(clock: FakeClock) -> tuple[SessionRegistry, ServerBus, ChatServer, ChatServer, ChatService]:
    registry = SessionRegistry(clock, ttl_s=15)
    bus = ServerBus()
    ws1, ws2 = ChatServer("ws-1", registry, bus), ChatServer("ws-2", registry, bus)
    chat = ChatService(registry, bus, clock)
    chat.create_conversation("duo", {"ann", "bob"})
    chat.create_conversation("trio", {"ann", "bob", "cat"})
    return registry, bus, ws1, ws2, chat


def test_send_sequences_and_routes_to_every_online_device_but_the_sender(world) -> None:
    _, _, ws1, ws2, chat = world
    ann_phone = ws1.connect("ann", "ann-phone")
    ws2.connect("ann", "ann-laptop")
    ws2.connect("bob", "bob-phone")
    first = chat.send(ann_phone, "duo", "c1", "hi")
    second = chat.send(ann_phone, "duo", "c2", "there")
    assert (first.seq, second.seq) == (1, 2)
    assert first.message_id == "duo:1"
    assert ws1.outbox("ann", "ann-phone") == []  # the sending socket is not echoed
    assert [m.body for m in ws2.outbox("ann", "ann-laptop")] == ["hi", "there"]  # multi-device
    assert [m.body for m in ws2.outbox("bob", "bob-phone")] == ["hi", "there"]  # cross-server
    assert chat.pushes() == []


def test_offline_recipient_gets_a_push_and_catches_up_with_sync(world) -> None:
    registry, _, ws1, _, chat = world
    ann_phone = ws1.connect("ann", "ann-phone")
    assert not registry.is_online("bob")
    msg = chat.send(ann_phone, "duo", "c1", "you there?")
    assert chat.pushes() == [("bob", msg.message_id)]
    ws1.connect("bob", "bob-phone")
    assert [m.body for m in chat.sync("bob", "duo", after_seq=0)] == ["you there?"]
    assert chat.sync("bob", "duo", after_seq=1) == []
    with pytest.raises(NotFoundError):
        chat.sync("zed", "duo", after_seq=0)


def test_retry_with_the_same_client_msg_id_is_idempotent(world) -> None:
    _, _, ws1, ws2, chat = world
    ann_phone = ws1.connect("ann", "ann-phone")
    ws2.connect("bob", "bob-phone")
    original = chat.send(ann_phone, "duo", "c1", "hi")
    retry = chat.send(ann_phone, "duo", "c1", "hi")
    assert retry == original
    assert len(ws2.outbox("bob", "bob-phone")) == 1
    assert chat.sync("bob", "duo", after_seq=0) == [original]


def test_delivery_state_aggregates_and_only_moves_forward(world) -> None:
    _, _, ws1, ws2, chat = world
    ann_phone = ws1.connect("ann", "ann-phone")
    ws2.connect("bob", "bob-phone")
    m1 = chat.send(ann_phone, "trio", "c1", "one")
    m2 = chat.send(ann_phone, "trio", "c2", "two")
    assert chat.status(m1.message_id) is DeliveryState.SENT
    chat.ack_delivered("bob", m1.message_id)
    assert chat.status(m1.message_id) is DeliveryState.SENT  # cat has not received it yet
    chat.ack_delivered("cat", m1.message_id)
    assert chat.status(m1.message_id) is DeliveryState.DELIVERED
    assert chat.mark_read("bob", "trio", up_to_seq=2) == 2  # read cursor covers m1 and m2
    assert chat.mark_read("bob", "trio", up_to_seq=2) == 0  # idempotent
    assert chat.ack_delivered("bob", m2.message_id) is DeliveryState.READ  # late ack cannot regress
    assert chat.status(m2.message_id) is DeliveryState.SENT  # cat still has nothing
    chat.ack_delivered("cat", m2.message_id)
    chat.mark_read("cat", "trio", up_to_seq=2)
    assert chat.status(m1.message_id) is DeliveryState.READ
    assert chat.status(m2.message_id) is DeliveryState.READ
    with pytest.raises(NotFoundError):
        chat.ack_delivered("ann", m1.message_id)  # the sender is not a recipient
    with pytest.raises(NotFoundError):
        chat.status("trio:99")


def test_dead_server_channel_falls_back_to_push_and_forgets_the_session(world) -> None:
    registry, bus, ws1, ws2, chat = world
    ann_phone = ws1.connect("ann", "ann-phone")
    ws2.connect("bob", "bob-phone")
    bus.unsubscribe("ws-2")  # ws-2 crashed; its registry entries are still there
    msg = chat.send(ann_phone, "duo", "c1", "hello?")
    assert chat.pushes() == [("bob", msg.message_id)]
    assert registry.sessions("bob") == []


def test_sessions_expire_when_their_server_stops_heartbeating(clock: FakeClock) -> None:
    registry = SessionRegistry(clock, ttl_s=15)
    bus = ServerBus()
    ws1 = ChatServer("ws-1", registry, bus)
    ws1.connect("ann", "ann-phone")
    clock.advance(10)
    ws1.heartbeat()
    clock.advance(10)
    assert registry.is_online("ann")  # 10 s since the last heartbeat
    clock.advance(6)
    assert not registry.is_online("ann")  # 16 s > ttl: the server is presumed dead
    with pytest.raises(ValidationError):
        registry.connect("bob", "bob-phone", "ws-never-seen")


def test_validation_errors(world) -> None:
    _, _, ws1, _, chat = world
    ann_phone = ws1.connect("ann", "ann-phone")
    with pytest.raises(ValidationError):
        chat.send(ann_phone, "duo", "c1", "   ")
    with pytest.raises(NotFoundError):
        chat.send(ann_phone, "no-such-conversation", "c1", "hi")
    with pytest.raises(ValidationError):
        chat.create_conversation("solo", {"ann"})


def test_concurrent_sends_get_unique_contiguous_seqs(world) -> None:
    _, _, ws1, ws2, chat = world
    ann_phone = ws1.connect("ann", "ann-phone")
    ws2.connect("bob", "bob-phone")
    with ThreadPoolExecutor(max_workers=8) as pool:
        sent = list(pool.map(lambda i: chat.send(ann_phone, "duo", f"c{i}", f"m{i}"), range(400)))
    assert sorted(m.seq for m in sent) == list(range(1, 401))
    delivered = ws2.outbox("bob", "bob-phone")
    assert len(delivered) == 400 and len({m.seq for m in delivered}) == 400
    assert [m.seq for m in chat.sync("bob", "duo", after_seq=0, limit=1000)] == list(range(1, 401))
