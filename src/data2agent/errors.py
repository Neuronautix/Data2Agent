"""Exception types shared across Data2Agent."""


class Data2AgentError(Exception):
    """Base class for all Data2Agent errors."""


class SourceError(Data2AgentError):
    """The dataset source is missing, unreadable, or not a directory."""


class OutputError(Data2AgentError):
    """The output directory is unusable, or does not hold an ingested dataset."""


class ModeError(Data2AgentError):
    """The requested benchmark mode is unknown or not available in this version."""
