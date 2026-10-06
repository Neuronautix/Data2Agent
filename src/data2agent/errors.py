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
