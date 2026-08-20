"""T: A 10-20 line scenario that prints what the page's "Run it" section shows."""

from lld.lld_problem.services import ExampleService


def main() -> None:
    service = ExampleService()
    entity = service.create()
    print(f"created {entity.id} with status {entity.status}")


if __name__ == "__main__":
    main()
