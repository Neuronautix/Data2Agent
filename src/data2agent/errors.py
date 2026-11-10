"""Exception types shared across Data2Agent."""


class Data2AgentError(Exception):
    """Base class for all Data2Agent errors."""


class SourceError(Data2AgentError):
    """The dataset source is missing, unreadable, or not a directory."""


class OutputError(Data2AgentError):
    """The output directory is unusable, or does not hold an ingested dataset."""


class ModeError(Data2AgentError):
    """The requested benchmark mode is unknown or not available in this version."""


class LayoutError(Data2AgentError):
    """A table layout declaration is malformed, or names a table that does not exist."""


class QueryError(Data2AgentError):
    """A well-formed deterministic dataset query cannot be executed."""


class QueryValidationError(QueryError, ValueError):
    """An anticipated query rejection, safe to expose and still a ValueError."""


class QueryLookupError(QueryValidationError, KeyError):
    """A query names a table, column or crosswalk the dataset does not have.

    Also a KeyError (and, through QueryValidationError, a ValueError) so callers
    that caught the KeyError these lookups used to raise keep working.
    """

    def __str__(self) -> str:
        # KeyError.__str__ would repr() the message; clients need it readable.
        return BaseException.__str__(self)


class LookupRejected(Data2AgentError, KeyError):
    """A caller named something the dataset or server does not have.

    An anticipated miss caused by the caller's own input (an unknown path, claim
    id, metadata file, principle, rule or snapshot record), so the MCP binding
    passes its message to the client instead of masking it as an internal crash.
    Still a KeyError, so callers that caught the KeyError these lookups used to
    raise keep working. The message names the caller's input (shortened by
    ``echo``) and server-side names only, never file contents.
    """

    def __str__(self) -> str:
        # KeyError.__str__ would repr() the message; clients need it readable.
        return BaseException.__str__(self)


ECHO_LIMIT = 120
NAMES_LIMIT = 20


def echo(value: object, limit: int = ECHO_LIMIT) -> str:
    """Caller input made safe to repeat in an error: one line, at most ``limit`` chars."""
    text = "".join(char if char.isprintable() else "?" for char in str(value))
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... ({len(text)} chars)"


def name_list(names: object, limit: int = NAMES_LIMIT) -> str:
    """A bounded, comma-separated list of server-side names for an error message."""
    items = [f"'{echo(name)}'" for name in names]  # type: ignore[attr-defined]
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", ... ({len(items) - limit} more)"
