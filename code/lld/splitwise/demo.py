"""A trip to Goa: four members, three split types, an edit, a simplification, a settlement."""

from common import FakeClock, Money, SequentialIdGenerator
from lld.splitwise.commands import AddExpenseCommand, CommandHistory
from lld.splitwise.models import Group, SplitType, User
from lld.splitwise.services import ActivityFeed, ExpenseService, SplitwiseStore

MEMBERS = ("alice", "bob", "carol", "dave")


def build(clock: FakeClock) -> tuple[ExpenseService, ActivityFeed]:
    store = SplitwiseStore()
    for name in MEMBERS:
        store.add_user(User(name, name.title(), f"{name}@example.com"))
    store.create_group(Group("goa", "Goa Trip", set(MEMBERS)))
    feed = ActivityFeed()
    service = ExpenseService(store, clock=clock, ids=SequentialIdGenerator("X"), listeners=[feed])
    return service, feed


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    service, feed = build(clock)
    history = CommandHistory()

    hotel = history.run(
        AddExpenseCommand(service, "goa", "Hotel", {"alice": Money.of("300.00")}, MEMBERS, actor_id="alice")
    )
    print(f"{hotel.id} Hotel {hotel.amount} paid by alice, equal: " + ", ".join(f"{s.user_id} {s.amount}" for s in hotel.owed_by))
    dinner = service.add_expense("goa", "Dinner", {"bob": Money.of("100.01")}, ("alice", "bob", "carol"), actor_id="bob")
    print(f"{dinner.id} Dinner {dinner.amount} paid by bob, equal over 3: " + ", ".join(f"{s.user_id} {s.amount}" for s in dinner.owed_by))
    cab = service.add_expense(
        "goa", "Cab", {"carol": Money.of("120.00")}, ("alice", "carol", "dave"),
        split_type=SplitType.PERCENT, weights=(5000, 2500, 2500), actor_id="carol",
    )
    print(f"{cab.id} Cab {cab.amount} paid by carol, percent 50/25/25: " + ", ".join(f"{s.user_id} {s.amount}" for s in cab.owed_by))

    print("balances: " + ", ".join(f"{u} {m}" for u, m in service.balances("goa").items()))
    clock.advance(3600)
    edited = service.edit_expense("goa", hotel.id, "Hotel", {"alice": Money.of("280.00")}, MEMBERS, actor_id="alice")
    print(f"edited {hotel.id} -> {edited.id}: hotel is now {edited.amount}")
    print("balances: " + ", ".join(f"{u} {m}" for u, m in service.balances("goa").items()))

    plan = service.simplify("goa")
    print(f"simplify: {len(plan)} transfers for 4 members")
    for transfer in plan:
        print(f"  {transfer.debtor_id} pays {transfer.creditor_id} {transfer.amount}")
    first = plan[0]
    service.settle_up("goa", first.debtor_id, first.creditor_id, first.amount)
    print(f"after {first.debtor_id} settles: " + ", ".join(f"{u} {m}" for u, m in service.balances("goa").items()))
    print(f"alice across every group: {service.global_balance('alice')}")
    print("--- activity feed ---")
    print(feed.render("goa", limit=4))


if __name__ == "__main__":
    main()
