"""Assessment profiles.

A profile is a registry of canonical rules plus deterministic implementations of
their checks. Profiles sit **on** the core and are never imported by it: nothing
in ``ingest/``, ``evidence/`` or the MCP service knows that FAIR exists.

That separation is the experimental control. The benchmark compares an agent
given FAIR as prose against one given FAIR as rules against one given FAIR as
executable checks -- a comparison that only means anything if the layer
underneath is identical in all three. If FAIR vocabulary leaks into the core,
``structured`` stops being a control condition.
"""

from .loader import load_profile
from .model import Assessment, CheckOutcome, ProfileContext, Rule, RuleResult

__all__ = [
    "Assessment",
    "CheckOutcome",
    "ProfileContext",
    "Rule",
    "RuleResult",
    "load_profile",
]
