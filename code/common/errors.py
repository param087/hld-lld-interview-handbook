"""Domain exception hierarchy shared by all handbook code."""


class HandbookError(Exception):
    """Base class for every domain error raised by handbook code."""


class ValidationError(HandbookError):
    """Input violates a precondition (bad argument, impossible request)."""


class NotFoundError(HandbookError):
    """The referenced entity does not exist."""


class ConflictError(HandbookError):
    """The operation conflicts with current state (double booking, duplicate key, version mismatch)."""


class InvalidStateError(HandbookError):
    """The operation is not allowed in the entity's current state."""
