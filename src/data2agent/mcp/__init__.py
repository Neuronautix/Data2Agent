"""The Data2MCP layer.

``service`` holds all behaviour and knows nothing about MCP or about any
particular coding-agent host; ``server`` is a thin binding that exposes it over
the MCP protocol. Keeping them apart is what makes the benchmark possible: the
same service can be driven from Claude Code, Codex, Goose, Pi, or a test.
"""

from .modes import MODES, Mode, resolve_mode
from .service import DatasetService

__all__ = ["MODES", "Mode", "DatasetService", "resolve_mode"]
