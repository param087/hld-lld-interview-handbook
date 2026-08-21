"""Interpreter: a small grammar, a tree of expression objects, and an evaluator per node.

The running example is a rule engine. Rules such as ``amount > 100 AND
country = "US"`` arrive as text (a config file, an admin screen), are tokenised
and parsed by ``Parser`` into a tree of ``Expression`` nodes, and every node
knows how to evaluate itself against a record (the Context). ``And``, ``Or`` and
``Not`` are the non-terminal nodes; ``Comparison`` is the leaf that reads one
field and compares it with a literal. The second half restates the evaluator as
one ``match`` statement over the same frozen dataclasses, which is also how you
add a second operation (which fields does a rule read?) without touching a node.
"""

from __future__ import annotations

import operator
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from common import NotFoundError, ValidationError

type Scalar = int | float | str
type Value = Scalar | tuple[Scalar, ...]


class ParseError(ValidationError):
    """The text is not a sentence of the grammar; the message says where it went wrong."""


# --8<-- [start:nodes]
class Expression(ABC):
    """The abstract expression: every node, leaf or not, evaluates itself against a record.

    An ``ABC`` so that the tree can be walked by ``isinstance`` and ``match`` alike;
    the concrete nodes are frozen dataclasses, so a parsed rule is a value with
    equality and a hash, which makes caching parsed rules by their text trivial.
    """

    @abstractmethod
    def evaluate(self, record: Mapping[str, object]) -> bool: ...


OPERATORS: Mapping[str, Callable[[object, object], bool]] = {
    "=": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


def lookup(record: Mapping[str, object], field: str) -> object:
    """The Context is a plain mapping; an unknown field is an error, never a silent False."""
    if field not in record:
        raise NotFoundError(f"record has no field {field!r}")
    return record[field]


def compare(op: str, actual: object, expected: Value) -> bool:
    try:
        if op == "IN":
            if not isinstance(expected, tuple):
                raise ValidationError("IN needs a list of values")
            return actual in expected
        return OPERATORS[op](actual, expected)
    except TypeError as exc:
        raise ValidationError(f"cannot apply {op} to {actual!r} and {expected!r}") from exc


@dataclass(frozen=True, slots=True)
class Comparison(Expression):
    """The terminal expression: one field of the record against one literal."""

    field: str
    op: str
    value: Value

    def evaluate(self, record: Mapping[str, object]) -> bool:
        return compare(self.op, lookup(record, self.field), self.value)


@dataclass(frozen=True, slots=True)
class And(Expression):
    left: Expression
    right: Expression

    def evaluate(self, record: Mapping[str, object]) -> bool:
        return self.left.evaluate(record) and self.right.evaluate(record)


@dataclass(frozen=True, slots=True)
class Or(Expression):
    left: Expression
    right: Expression

    def evaluate(self, record: Mapping[str, object]) -> bool:
        return self.left.evaluate(record) or self.right.evaluate(record)


@dataclass(frozen=True, slots=True)
class Not(Expression):
    inner: Expression

    def evaluate(self, record: Mapping[str, object]) -> bool:
        return not self.inner.evaluate(record)


# --8<-- [end:nodes]


# --8<-- [start:parser]
# The grammar, lowest precedence first. One method of ``Parser`` per line.
#   expr       := and_expr ("OR" and_expr)*
#   and_expr   := unary ("AND" unary)*
#   unary      := "NOT" unary | "(" expr ")" | comparison
#   comparison := FIELD op SCALAR | FIELD "IN" "(" SCALAR ("," SCALAR)* ")"
#   op         := "=" | "!=" | "<" | "<=" | ">" | ">="
KEYWORDS = frozenset({"AND", "OR", "NOT", "IN"})
TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<string>"[^"]*")
  | (?P<unterminated>"[^"]*$)
  | (?P<op><=|>=|!=|=|<|>)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<comma>,)
  | (?P<word>[A-Za-z_][A-Za-z0-9_.]*)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    text: str
    position: int


def tokenize(text: str) -> list[Token]:
    """Split the text into tokens; keywords are case-insensitive, everything else is literal."""
    tokens: list[Token] = []
    position = 0
    while position < len(text):
        matched = TOKEN_RE.match(text, position)
        if matched is None:
            raise ParseError(f"unexpected character {text[position]!r} at position {position}")
        kind, lexeme = matched.lastgroup or "", matched.group()
        if kind == "unterminated":
            raise ParseError(f"unterminated string at position {position}")
        if kind == "word" and lexeme.upper() in KEYWORDS:
            kind, lexeme = "keyword", lexeme.upper()
        if kind != "ws":
            tokens.append(Token(kind, lexeme, position))
        position = matched.end()
    tokens.append(Token("eof", "", len(text)))
    return tokens


class Parser:
    """Recursive descent: one method per grammar rule; the token index is the only state.

    Precedence is the call structure: ``_or`` calls ``_and`` which calls ``_unary``,
    so ``a OR b AND c`` parses as ``a OR (b AND c)`` without a precedence table.
    """

    def __init__(self, tokens: Sequence[Token]) -> None:
        self._tokens = tokens
        self._pos = 0

    def parse(self) -> Expression:
        expression = self._or()
        self._expect("eof", "end of input")
        return expression

    @property
    def _current(self) -> Token:
        return self._tokens[self._pos]

    def _accept(self, kind: str, text: str | None = None) -> Token | None:
        token = self._current
        if token.kind == kind and (text is None or token.text == text):
            self._pos += 1
            return token
        return None

    def _expect(self, kind: str, what: str) -> Token:
        token = self._accept(kind)
        if token is None:
            raise self._fail(what)
        return token

    def _fail(self, what: str) -> ParseError:
        token = self._current
        found = "end of input" if token.kind == "eof" else repr(token.text)
        return ParseError(f"expected {what} at position {token.position}, found {found}")

    def _or(self) -> Expression:
        left = self._and()
        while self._accept("keyword", "OR"):
            left = Or(left, self._and())
        return left

    def _and(self) -> Expression:
        left = self._unary()
        while self._accept("keyword", "AND"):
            left = And(left, self._unary())
        return left

    def _unary(self) -> Expression:
        if self._accept("keyword", "NOT"):
            return Not(self._unary())
        if self._accept("lparen"):
            inner = self._or()
            self._expect("rparen", "')'")
            return inner
        return self._comparison()

    def _comparison(self) -> Expression:
        field = self._expect("word", "a field name").text
        if self._accept("keyword", "IN"):
            return Comparison(field, "IN", self._values())
        op = self._expect("op", "a comparison operator").text
        return Comparison(field, op, self._scalar())

    def _values(self) -> tuple[Scalar, ...]:
        self._expect("lparen", "'('")
        items = [self._scalar()]
        while self._accept("comma"):
            items.append(self._scalar())
        self._expect("rparen", "')'")
        return tuple(items)

    def _scalar(self) -> Scalar:
        if token := self._accept("number"):
            return float(token.text) if "." in token.text else int(token.text)
        if token := self._accept("string"):
            return token.text[1:-1]
        raise self._fail("a value")


def parse(text: str) -> Expression:
    """Text in, tree out. Parse once at save time; evaluate the tree per record."""
    return Parser(tokenize(text)).parse()


# --8<-- [end:parser]


# --8<-- [start:match_form]
def evaluate(node: Expression, record: Mapping[str, object]) -> bool:
    """The evaluator as one function: ``match`` on the node type instead of a method per class.

    Same tree, same answers. The difference is where the next operation goes: a new
    function here, a new method on every node there (the Visitor trade-off).
    """
    match node:
        case Comparison(field, op, value):
            return compare(op, lookup(record, field), value)
        case And(left, right):
            return evaluate(left, record) and evaluate(right, record)
        case Or(left, right):
            return evaluate(left, record) or evaluate(right, record)
        case Not(inner):
            return not evaluate(inner, record)
    raise TypeError(f"not an expression node: {node!r}")


def fields_read(node: Expression) -> frozenset[str]:
    """A second operation over the same tree, without touching a node: what does the rule read?"""
    match node:
        case Comparison(field, _, _):
            return frozenset({field})
        case And(left, right) | Or(left, right):
            return fields_read(left) | fields_read(right)
        case Not(inner):
            return fields_read(inner)
    raise TypeError(f"not an expression node: {node!r}")


# --8<-- [end:match_form]


def main() -> None:
    rules = {
        "big US order": 'amount > 100 AND country = "US"',
        "risky": 'NOT country IN ("US", "CA") OR attempts >= 3',
        "vip or bulk": 'tier = "gold" OR (quantity >= 10 AND amount > 500)',
    }
    records = [
        {"amount": 250, "country": "US", "attempts": 1, "tier": "silver", "quantity": 2},
        {"amount": 80, "country": "DE", "attempts": 1, "tier": "gold", "quantity": 1},
        {"amount": 900, "country": "CA", "attempts": 4, "tier": "bronze", "quantity": 12},
    ]
    print("--- the rule as text, and the tree the parser builds from it ---")
    print(rules["big US order"])
    print(parse(rules["big US order"]))

    trees = {name: parse(text) for name, text in rules.items()}
    print("--- three rules against three records ---")
    print(f"{'record':<34}" + "".join(f"{name:>14}" for name in rules))
    for record in records:
        label = f"amount={record['amount']} country={record['country']} tier={record['tier']}"
        verdicts = "".join(f"{'yes' if tree.evaluate(record) else 'no':>14}" for tree in trees.values())
        print(f"{label:<34}{verdicts}")

    print("--- the tree is data: a second operation without touching the nodes ---")
    print(f"fields read by 'risky': {', '.join(sorted(fields_read(trees['risky'])))}")
    print(f"parsed rules are values: parse(text) == parse(text) -> {parse(rules['risky']) == trees['risky']}")

    print("--- errors say where and why ---")
    for bad in ("amount > AND", 'country = "US', "(amount > 1"):
        try:
            parse(bad)
        except ParseError as exc:
            print(f"parse error in {bad!r}: {exc}")
    try:
        trees["vip or bulk"].evaluate({"amount": 1})
    except NotFoundError as exc:
        print(f"evaluation error: {exc}")

    agree = all(
        tree.evaluate(record) == evaluate(tree, record) for tree in trees.values() for record in records
    )
    print(f"--- the match-statement evaluator agrees with the methods on all 9 cases: {agree} ---")


if __name__ == "__main__":
    main()
