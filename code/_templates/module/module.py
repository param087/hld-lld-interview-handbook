"""T: Single-file artifact (HLD algorithm, design pattern, fundamentals example).

T: Use `# --8<-- [start:name]` / `# --8<-- [end:name]` markers around the parts the page embeds.
"""

from __future__ import annotations


# --8<-- [start:core]
class Example:
    def run(self) -> str:
        return "ok"


# --8<-- [end:core]


def main() -> None:
    print(Example().run())


if __name__ == "__main__":
    main()
