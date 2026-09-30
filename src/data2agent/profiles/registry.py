"""Which deterministic checks belong to which profile.

A profile is a directory of canonical rules (``profiles/<id>/rules/*.yaml``) plus a
package ``data2agent.profiles.<id>`` that exports ``CHECKS``: a mapping from a
rule's ``check.type`` to its implementation. Resolving that mapping by name, here,
is what lets ``data2agent assess --profile <id>`` run any profile without the CLI or
the service importing each one -- and keeps every profile optional, so nothing in
the core ever depends on one.
"""

from __future__ import annotations

import importlib
import re
from typing import TYPE_CHECKING

from ..errors import Data2AgentError

if TYPE_CHECKING:
    from .runner import CheckImplementation

# A profile id becomes a package name, so it must be a plain identifier. This also
# keeps a caller-supplied id from steering the import to an arbitrary module.
_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class UnknownProfileError(Data2AgentError):
    """No profile with this id ships with Data2Agent."""


def checks_for(profile_id: str) -> dict[str, CheckImplementation]:
    """Return the ``CHECKS`` registry of a shipped profile."""
    if not _PROFILE_ID.fullmatch(profile_id):
        raise UnknownProfileError(
            f"'{profile_id}' is not a valid profile id (lower-case letters, digits, underscore)"
        )
    module_name = f"{__package__}.{profile_id}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name != module_name:
            raise
        raise UnknownProfileError(
            f"no profile named '{profile_id}' ships with Data2Agent"
        ) from error
    checks = getattr(module, "CHECKS", None)
    if not isinstance(checks, dict):
        raise UnknownProfileError(f"profile '{profile_id}' does not export a CHECKS mapping")
    return checks
