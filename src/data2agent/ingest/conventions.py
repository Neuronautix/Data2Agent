"""Missing-value conventions.

A blank cell records no value. A cell holding ``NA`` records a *token*, and
whether that token means "missing" is a property of the dataset's conventions,
not of the bytes. Data2Agent therefore resolves such tokens through an explicit,
named convention rather than by judgement: the convention is data, it travels
with the manifest, and every missingness claim cites it.

That distinction is what keeps "NA means missing" a declared rule rather than an
inference. Change the convention and the numbers change, visibly, with the
reason attached.

Three classes of token:

``sentinel``
    Tokens the active convention resolves to missing. Counted in ``missing``.
``ambiguous``
    Tokens that often *look* like absence but are not standard sentinels --
    ``unknown``, ``-``, ``?``. These are counted and reported, never resolved:
    a cell reading ``unknown`` may well be a deliberate, meaningful statement.
``value``
    Everything else.
"""

from __future__ import annotations

from dataclasses import dataclass

# Aligned with the sentinel set pandas has used for years, which is the closest
# thing to a cross-tool default convention that exists. Deliberately narrower
# than "anything that looks empty".
DEFAULT_SENTINELS: frozenset[str] = frozenset(
    {"na", "n/a", "#n/a", "#n/a n/a", "#na", "nan", "-nan", "<na>", "null", "none", "nil"}
)

# Never resolved to missing by any built-in convention. A dataset that means
# these to be missing must say so, at which point they become sentinels.
AMBIGUOUS_TOKENS: frozenset[str] = frozenset(
    {".", "-", "--", "?", "unknown", "unspecified", "not recorded", "no data", "n.a.", "tbd"}
)

SENTINEL = "sentinel"
AMBIGUOUS = "ambiguous"
VALUE = "value"


@dataclass(frozen=True)
class MissingValueConvention:
    """Which tokens count as missing, and on whose authority."""

    id: str
    tokens: frozenset[str]
    source: str  # where the convention came from, in plain words
    case_sensitive: bool = False

    def classify(self, value: str) -> str:
        """Classify a non-empty, stripped cell value."""
        candidate = value if self.case_sensitive else value.lower()
        if candidate in self.tokens:
            return SENTINEL
        if candidate.lower() in AMBIGUOUS_TOKENS:
            return AMBIGUOUS
        return VALUE

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source": self.source,
            "tokens": sorted(self.tokens),
            "case_sensitive": self.case_sensitive,
            # Stated so no reader has to infer it from the token list.
            "ambiguous_tokens_resolved": False,
        }


DEFAULT_CONVENTION = MissingValueConvention(
    id="default-sentinels",
    tokens=DEFAULT_SENTINELS,
    source="built-in default (the dataset declares no convention of its own)",
)

STRICT_CONVENTION = MissingValueConvention(
    id="strict-empty-only",
    tokens=frozenset(),
    source="built-in strict convention: only an empty cell is missing",
)


def custom(
    tokens: list[str], *, source: str = "supplied on the command line"
) -> MissingValueConvention:
    """Build a convention from an explicit token list."""
    normalised = frozenset(token.strip().lower() for token in tokens if token.strip())
    return MissingValueConvention(id="custom", tokens=normalised, source=source)


def from_datapackage(descriptor: dict, *, path: str) -> MissingValueConvention | None:
    """Read a Frictionless Data Package's declared ``missingValues``, if present.

    This is the good case: the dataset states its own convention, so nothing has
    to be assumed. Both the top-level and the first resource-level declaration
    are honoured, resource-level winning, matching the Frictionless spec.
    """
    declared = None
    location = path

    top_level = descriptor.get("missingValues")
    if isinstance(top_level, list):
        declared = top_level

    for resource in descriptor.get("resources", []) or []:
        if not isinstance(resource, dict):
            continue
        schema = resource.get("schema")
        if isinstance(schema, dict) and isinstance(schema.get("missingValues"), list):
            declared = schema["missingValues"]
            location = f"{path} (resource '{resource.get('name', '?')}')"
            break

    if declared is None:
        return None

    # Frictionless includes "" in missingValues to mean the empty cell, which we
    # already handle structurally; dropping it keeps the token list meaningful.
    tokens = frozenset(str(token).strip().lower() for token in declared if str(token).strip())
    return MissingValueConvention(
        id="declared", tokens=tokens, source=f"declared by the dataset in {location}"
    )
