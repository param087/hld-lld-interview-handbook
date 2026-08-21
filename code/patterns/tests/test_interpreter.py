"""Interpreter: text becomes a tree, the tree evaluates itself, and a match statement can do the same."""

import pytest

from common import NotFoundError, ValidationError
from patterns.interpreter import (
    And,
    Comparison,
    Not,
    Or,
    ParseError,
    Token,
    evaluate,
    fields_read,
    parse,
    tokenize,
)

RECORD: dict[str, object] = {"amount": 250, "country": "US", "attempts": 1, "tier": "gold", "score": 4.5}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("amount > 100", True),
        ("amount >= 250", True),
        ("amount < 250", False),
        ("amount <= 250", True),
        ('country = "US"', True),
        ('country != "US"', False),
        ('country IN ("US", "CA")', True),
        ('country in ("DE", "FR")', False),  # keywords are case-insensitive
        ("score > 4.25", True),
        ('amount > 100 AND country = "DE"', False),
        ('amount > 100 OR country = "DE"', True),
        ('NOT country = "US"', False),
        ("NOT NOT amount > 100", True),
        # AND binds tighter than OR, NOT tighter than both, parentheses override
        ('country = "DE" OR amount > 100 AND attempts < 2', True),
        ('(country = "DE" OR amount > 100) AND attempts > 2', False),
        ('NOT country = "DE" AND amount > 100', True),
        ('NOT (country = "DE" AND amount > 100)', True),
    ],
)
def test_rules_evaluate_with_the_expected_precedence(text: str, expected: bool) -> None:
    tree = parse(text)
    assert tree.evaluate(RECORD) is expected
    assert evaluate(tree, RECORD) is expected  # the match-statement evaluator agrees


def test_the_parser_builds_the_tree_the_grammar_says_and_the_tree_is_a_value() -> None:
    tree = parse('NOT country IN ("US", "CA") OR attempts >= 3 AND amount > 100')
    assert tree == Or(
        Not(Comparison("country", "IN", ("US", "CA"))),
        And(Comparison("attempts", ">=", 3), Comparison("amount", ">", 100)),
    )
    assert parse("amount > 100") == parse("amount   >   100")
    cache = {parse("amount > 100"): "big"}  # frozen dataclasses are hashable
    assert cache[parse("amount > 100")] == "big"


def test_tokens_carry_their_kind_text_and_position() -> None:
    assert tokenize('a >= "x"') == [
        Token("word", "a", 0),
        Token("op", ">=", 2),
        Token("string", '"x"', 5),
        Token("eof", "", 8),
    ]
    assert [token.kind for token in tokenize("x and y")] == ["word", "keyword", "word", "eof"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("amount > AND", "expected a value at position 9, found 'AND'"),
        ("amount >", "expected a value at position 8, found end of input"),
        ("(amount > 1", "expected ')' at position 11, found end of input"),
        ("amount > 1)", "expected end of input at position 10, found ')'"),
        ("amount 1", "expected a comparison operator at position 7, found '1'"),
        ("> 1", "expected a field name at position 0, found '>'"),
        ('country = "US', "unterminated string at position 10"),
        ("amount @ 1", "unexpected character '@' at position 7"),
        ("country IN US", "expected '(' at position 11, found 'US'"),
        ("", "expected a field name at position 0, found end of input"),
    ],
)
def test_parse_errors_say_where_and_why(text: str, message: str) -> None:
    with pytest.raises(ParseError, match=message.replace("(", r"\(").replace(")", r"\)")) as info:
        parse(text)
    assert isinstance(info.value, ValidationError)


def test_evaluation_errors_are_distinct_from_parse_errors() -> None:
    tree = parse('tier = "gold" AND amount > 100')
    with pytest.raises(NotFoundError, match="no field 'tier'"):
        tree.evaluate({"amount": 500})
    with pytest.raises(NotFoundError):
        evaluate(tree, {"amount": 500})
    with pytest.raises(ValidationError, match="cannot apply >"):
        tree.evaluate({"tier": "gold", "amount": "lots"})
    with pytest.raises(ValidationError, match="IN needs a list"):
        Comparison("country", "IN", "US").evaluate(RECORD)


def test_short_circuit_means_the_right_side_is_not_consulted() -> None:
    # the left side decides, so the missing field on the right never raises
    assert parse('country = "US" OR missing > 1').evaluate(RECORD) is True
    assert parse('country = "DE" AND missing > 1').evaluate(RECORD) is False
    with pytest.raises(NotFoundError):
        parse('country = "DE" OR missing > 1').evaluate(RECORD)


def test_a_second_operation_walks_the_same_tree_without_touching_the_nodes() -> None:
    tree = parse('NOT country IN ("US", "CA") OR attempts >= 3 AND amount > 100')
    assert fields_read(tree) == {"country", "attempts", "amount"}
    assert fields_read(parse("amount > 1 AND amount < 9")) == {"amount"}
    with pytest.raises(TypeError):
        fields_read("not a node")  # type: ignore[arg-type]
