"""A week at the front desk: search, reserve, pay, check in, check out, cancel."""

from datetime import date

from common import FakeClock, Money, SequentialIdGenerator
from lld.hotel_management.hotel import HotelBuilder
from lld.hotel_management.models import (
    DateRange,
    NoAvailabilityError,
    PaymentMethod,
    RoomRequest,
    RoomType,
    Staff,
    StaffRole,
)
from lld.hotel_management.ports import (
    AlwaysApprovesGateway,
    HousekeepingService,
    NotificationService,
)
from lld.hotel_management.services import AvailabilityService, FrontDeskService

TODAY_EPOCH = 1_773_219_600.0  # 2026-03-11 09:00 UTC


def main() -> None:
    clock = FakeClock(start=TODAY_EPOCH)
    hotel = (
        HotelBuilder()
        .named("Seaside Grand")
        .with_rooms(RoomType.DOUBLE, 4, floor=1)
        .with_rooms(RoomType.DELUXE, 2, floor=2)
        .with_rooms(RoomType.SUITE, 1, floor=3)
        .build()
    )
    availability = AvailabilityService(hotel)
    gateway = AlwaysApprovesGateway()
    housekeeping = HousekeepingService(clock=clock, ids=SequentialIdGenerator("HK"))
    notifier = NotificationService()
    desk = FrontDeskService(
        hotel,
        availability,
        gateway=gateway,
        clock=clock,
        ids=SequentialIdGenerator("RSV"),
        payment_ids=SequentialIdGenerator("PAY"),
    )
    desk.subscribe(housekeeping)
    desk.subscribe(notifier)

    stay_a = DateRange(date(2026, 3, 11), date(2026, 3, 14))
    stay_b = DateRange(date(2026, 3, 13), date(2026, 3, 16))
    stay_c = DateRange(date(2026, 3, 9), date(2026, 3, 11))
    inventory = {str(k): v for k, v in sorted(hotel.inventory().items())}
    print(f"{hotel.name}: {len(hotel.all_rooms())} rooms, inventory {inventory}")
    print(f"doubles free {stay_a}: {desk.search(RoomType.DOUBLE, stay_a)}")

    first = desk.reserve("g-asha", [RoomRequest(RoomType.DOUBLE, 2)], stay_a)
    print(f"{first.id} {stay_a} 2 x double -> {first.status}, {first.amount}")
    try:
        desk.reserve("g-bala", [RoomRequest(RoomType.DOUBLE, 3)], stay_b)
    except NoAvailabilityError as exc:
        print(f"overlapping request rejected: {exc}")
    second = desk.reserve("g-bala", [RoomRequest(RoomType.DOUBLE, 2)], stay_b)
    print(f"{second.id} {stay_b} 2 x double -> {second.status} (nights 13 and 14 were free)")

    desk.pay(first.id, PaymentMethod.CARD, idempotency_key="key-asha")
    desk.pay(first.id, PaymentMethod.CARD, idempotency_key="key-asha")
    desk.pay(second.id, PaymentMethod.CARD, idempotency_key="key-bala")
    print(f"{first.id} paid once, replay is a no-op -> {first.status}")
    stale = desk.reserve("g-chitra", [RoomRequest(RoomType.SUITE, 1)], stay_c)
    desk.pay(stale.id, PaymentMethod.CASH, idempotency_key="key-chitra")
    print(f"no-show sweep on 2026-03-11 -> {desk.sweep_no_shows()}, suite back on sale")

    print(f"{first.id} checked in to rooms {','.join(desk.check_in(first.id))}")
    desk.add_charge(first.id, "minibar", Money.of("45.50"))
    invoice = desk.check_out(first.id)
    for line in invoice.lines:
        print(f"  {line.description}: {line.amount}")
    print(f"  tax 12%: {invoice.tax} -> total {invoice.total}")

    tasks = housekeeping.open_tasks()
    print(f"housekeeping raised {[(t.room_number, str(t.kind)) for t in tasks]}")
    first_task = tasks[0]
    housekeeping.assign(first_task.id, Staff("s-1", "Ravi", StaffRole.HOUSEKEEPER))
    housekeeping.complete(first_task.id)
    hotel.room(first_task.room_number).mark_clean()
    print(f"room {first_task.room_number} is {hotel.room(first_task.room_number).status} again")
    print(f"{second.id} cancelled 2 days out -> refund {desk.cancel(second.id)}")
    print(f"doubles free {stay_b} after the cancellation: {desk.search(RoomType.DOUBLE, stay_b)}")
    print(f"notifications: {len(notifier.outbox())} sent, last was {notifier.outbox()[-1]}")


if __name__ == "__main__":
    main()
