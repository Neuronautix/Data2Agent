"""Data2Agent assessments as FAIR-VCG *proposed* annotations.

The FAIR-VCG dataspace is a separate repository with its own rules (see
``docs/annotation-export-contract.md`` for the ones this module relies on). The
short version: whatever a tool produces is a **proposal**. It carries
``provenance.method: agent_proposal`` and ``review.status: draft``, it is written
only to ``<dataspace>/annotations/proposed/``, and it never touches
``annotations/curated/`` or ``data/original/``. This module enforces that in
code, not by convention:

* every record is built, checked against the invariants below, and (when the
  dataspace ships its schema and ``jsonschema`` is installed) validated against
  the real schema **before any file is written** -- one bad record aborts the
  whole export;
* a record that is not a draft agent proposal is a hard internal error, never
  silently corrected;
* an existing file is never overwritten unless ``overwrite`` is set, and even
  then never one a reviewer has moved past ``draft``.

No language model is involved anywhere: the verdicts were computed by code in the
assessment, and this module only re-expresses them.

``jsonschema`` is a dev-only dependency and is imported lazily; without it (or
without the dataspace's schema file) the export still runs, says so loudly, and
records ``schema_validated: false`` in its summary.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .. import __version__
from ..errors import Data2AgentError

SCHEMA_VERSION = "0.1.0"
PROPOSED_SUBPATH = ("annotations", "proposed")
SCHEMA_RELATIVE_PATH = ("schemas", "annotation.schema.json")

# -- errors -----------------------------------------------------------------------


class AnnotationExportError(Data2AgentError):
    """The export cannot proceed; nothing was written."""


class ExportRefused(AnnotationExportError):
    """A safety rule of the dataspace would be broken. Nothing was written."""


class ExportInternalError(AnnotationExportError):
    """This module produced a record that breaks a contract invariant. A bug, never data."""


# -- label mapping ----------------------------------------------------------------

#: Data2Agent result -> dataspace judgement label. ``partial`` is not a result the
#: shipped profiles can emit (the runner allows four outcomes); it is mapped so a
#: model-authored assessment that uses it is not rejected or guessed at.
LABEL_BY_RESULT: dict[str, str] = {
    "pass": "conformant",
    "fail": "non_conformant",
    "partial": "partial",
    "unknown": "insufficient_evidence",
    "not_applicable": "not_applicable",
}

#: Dataspace label -> Data2Agent result. ``partial`` has no Data2Agent equivalent and
#: is returned as itself; ``insufficient_evidence`` is ``unknown``, never a ``fail``.
RESULT_BY_LABEL: dict[str, str] = {
    "conformant": "pass",
    "non_conformant": "fail",
    "partial": "partial",
    "insufficient_evidence": "unknown",
    "not_applicable": "not_applicable",
}

_PARTIAL_AS = ("pass", "fail", "unknown")


def label_for_result(result: str) -> str:
    """Dataspace label for a Data2Agent result. Raises on anything not in the table."""
    try:
        return LABEL_BY_RESULT[result]
    except KeyError:
        raise AnnotationExportError(
            f"cannot map result {result!r} to a dataspace label; known: {sorted(LABEL_BY_RESULT)}"
        ) from None


def result_for_label(label: str, *, partial_as: str | None = None) -> str:
    """Data2Agent result for a dataspace label (the reverse direction, for scorers).

    ``insufficient_evidence`` becomes ``unknown`` -- it is *not* folded into ``fail``.
    ``partial`` has no Data2Agent equivalent, so it stays ``"partial"`` unless the
    caller states how their scoring treats it via ``partial_as`` (``pass``, ``fail``
    or ``unknown``): the collapse is the scorer's decision, made explicitly.
    """
    try:
        result = RESULT_BY_LABEL[label]
    except KeyError:
        raise AnnotationExportError(
            f"unknown dataspace label {label!r}; known: {sorted(RESULT_BY_LABEL)}"
        ) from None
    if result == "partial" and partial_as is not None:
        if partial_as not in _PARTIAL_AS:
            raise AnnotationExportError(
                f"partial_as must be one of {_PARTIAL_AS}, not {partial_as!r}"
            )
        return partial_as
    return result


FAIR_DIMENSIONS = {
    "F": "findable",
    "A": "accessible",
    "I": "interoperable",
    "R": "reusable",
}
# A whole-string match on purpose: "F1", "A1.1", "R1.3" are FAIR principles, while a
# profile's own category label that merely *starts* with one of the letters
# ("Reproducibility", "Item-3") is not, and is skipped rather than guessed.
_FAIR_PRINCIPLE = re.compile(r"^([FAIR])(?:\d+(?:\.\d+)*)?$")

QUALITY_PROFILE = "quality"
_QUALITY_REPORTED = ("fail", "partial", "unknown")

# -- patterns lifted from the dataspace schema (re-stated, not copied) ---------------

_LOCATOR = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/\-#\[\]=]+$")
_ANNOTATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ID_FRAGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_ASSESSMENT_FILE = re.compile(r"^assessment(?:\.([a-z][a-z0-9_]*))?\.json$")

_LABELS = frozenset(LABEL_BY_RESULT.values())
_TYPES = frozenset(
    {"fair_assessment", "quality_issue", "expected_improvement", "curation_validation"}
)
_KINDS = frozenset({"dataset", "file", "record", "field", "metadata_element"})
_MAX_CLAIMS_IN_DESCRIPTION = 3
_MAX_TEXT = 240


def _squash(text: Any) -> str:
    """Whitespace-normalised text. YAML-folded rationales end in a newline."""
    return " ".join(str(text).split())


def _clip(text: str, limit: int = _MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


# -- safety: paths ----------------------------------------------------------------


def resolve_dataspace(dataspace: Path | str) -> Path:
    root = Path(dataspace).expanduser()
    if not root.is_dir():
        raise ExportRefused(f"--dataspace {str(dataspace)!r} is not a directory")
    return root.resolve()


def validate_dataset_path(dataspace: Path, raw: str) -> str:
    """Refuse an absolute, traversing, missing or non-locator ``--dataset-path``.

    Returns the normalised posix path (no trailing slash). The path becomes both the
    annotation ``target.path`` and the prefix of every evidence locator, so it must
    satisfy the dataspace locator pattern itself, and it must be a real directory
    *below* the dataspace root.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ExportRefused("--dataset-path is required and must not be empty")
    value = raw.strip()
    if (
        value.startswith(("/", "\\"))
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or PureWindowsPath(value).drive
    ):
        raise ExportRefused(
            f"--dataset-path {raw!r} is absolute; it must be relative to --dataspace"
        )
    if "\\" in value:
        raise ExportRefused(f"--dataset-path {raw!r} contains a backslash; use forward slashes")
    if ".." in value:
        raise ExportRefused(f"--dataset-path {raw!r} contains '..'; it may not leave the dataspace")
    normalised = value.rstrip("/")
    segments = normalised.split("/")
    if not normalised or any(segment in ("", ".") for segment in segments):
        raise ExportRefused(
            f"--dataset-path {raw!r} must name a directory below the dataspace root "
            "(no empty or '.' segments)"
        )
    if not _LOCATOR.fullmatch(normalised):
        raise ExportRefused(
            f"--dataset-path {raw!r} uses characters the dataspace locator pattern forbids "
            "(allowed: A-Z a-z 0-9 . _ / - # [ ] =)"
        )
    target = (dataspace / normalised).resolve()
    if not target.is_dir():
        raise ExportRefused(
            f"--dataset-path {raw!r} does not exist as a directory under {dataspace}"
        )
    if target == dataspace or not target.is_relative_to(dataspace):
        raise ExportRefused(f"--dataset-path {raw!r} resolves outside the dataspace")
    return normalised


def proposed_dir(dataspace: Path) -> Path:
    """The one directory an export may write to. Must already exist."""
    expected = dataspace.joinpath(*PROPOSED_SUBPATH)
    if not expected.is_dir():
        raise ExportRefused(
            f"{expected} does not exist; refusing to create the dataspace's annotation folders"
        )
    _require_exact_output_dir(expected, dataspace)
    return expected


def _require_exact_output_dir(output_dir: Path, dataspace: Path) -> None:
    expected = dataspace.resolve().joinpath(*PROPOSED_SUBPATH)
    if output_dir.resolve() != expected:
        raise ExportRefused(
            f"resolved output directory {output_dir.resolve()} is not exactly {expected}; "
            "agent output goes to annotations/proposed/ and nowhere else"
        )


# -- inputs ----------------------------------------------------------------------


@dataclass
class _Context:
    manifest: dict[str, Any]
    dataset_id: str
    files: dict[str, dict[str, Any]]
    claims: dict[str, dict[str, Any]]
    family: str
    variant: str
    dataset_path: str
    dataspace: Path | None
    warnings: list[str] = field(default_factory=list)
    _present: dict[str, bool] = field(default_factory=dict)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AnnotationExportError(f"{what} not found at {path}") from None
    except (OSError, ValueError) as error:
        raise AnnotationExportError(f"{what} at {path} cannot be read as JSON: {error}") from None


def _load_context(
    ingest_dir: Path, family: str, variant: str, dataset_path: str, dataspace: Path | None
) -> _Context:
    manifest = _read_json(ingest_dir / "manifest.json", "manifest.json")
    dataset_id = manifest.get("dataset_id") if isinstance(manifest, dict) else None
    if not isinstance(dataset_id, str) or not dataset_id:
        raise AnnotationExportError(f"{ingest_dir / 'manifest.json'} has no dataset_id")
    files: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("files", []):
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            files[entry["path"]] = entry
    claims: dict[str, dict[str, Any]] = {}
    context = _Context(
        manifest, dataset_id, files, claims, family, variant, dataset_path, dataspace
    )
    evidence_path = ingest_dir / "evidence.json"
    if evidence_path.is_file():
        ledger = _read_json(evidence_path, "evidence.json")
        if ledger.get("dataset_id") not in (None, dataset_id):
            raise AnnotationExportError(
                "evidence.json and manifest.json describe different datasets "
                f"({ledger.get('dataset_id')} vs {dataset_id})"
            )
        for claim in ledger.get("claims", []):
            if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str):
                claims[claim["claim_id"]] = claim
    else:
        context.warn("evidence.json is missing: cited claim ids cannot be resolved to files")
    return context


def _load_assessments(ingest_dir: Path, dataset_id: str) -> list[dict[str, Any]]:
    """Every ``assessment[.<profile>].json``, ``assessment.json`` first, then by name."""
    found = []
    for path in sorted(ingest_dir.iterdir(), key=lambda p: (p.name != "assessment.json", p.name)):
        match = _ASSESSMENT_FILE.match(path.name)
        if not match or not path.is_file():
            continue
        payload = _read_json(path, path.name)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise AnnotationExportError(f"{path.name} is not a Data2Agent assessment (no results)")
        profile = payload.get("profile")
        if not isinstance(profile, dict) or not isinstance(profile.get("id"), str):
            raise AnnotationExportError(f"{path.name} does not state its profile id")
        if match.group(1) and match.group(1) != profile["id"]:
            raise AnnotationExportError(
                f"{path.name} claims profile {profile['id']!r}, which contradicts its file name"
            )
        if payload.get("dataset_id") != dataset_id:
            raise AnnotationExportError(
                f"{path.name} is for dataset {payload.get('dataset_id')!r}, but the ingest output "
                f"is {dataset_id!r}; re-run 'assess' on this ingest output"
            )
        found.append(payload)
    ids = [item["profile"]["id"] for item in found]
    duplicates = sorted({pid for pid in ids if ids.count(pid) > 1})
    if duplicates:
        raise AnnotationExportError(f"more than one assessment file states profile {duplicates}")
    return found


def _load_profile(profile_id: str, version: Any, profiles_root: Path | None, ctx: _Context):
    if not _PROFILE_ID.fullmatch(profile_id):
        raise AnnotationExportError(f"{profile_id!r} is not a valid profile id")
    try:
        from ..profiles.loader import load_profile

        profile = load_profile(profile_id, root=profiles_root)
    except ImportError as error:
        raise AnnotationExportError(
            f"cannot load profile {profile_id!r} to read its rule questions: {error}"
        ) from error
    except Data2AgentError as error:
        raise AnnotationExportError(
            f"cannot load profile {profile_id!r} to read its rule questions ({error}); the "
            "criterion description is never guessed, so this profile cannot be exported"
        ) from error
    if version is not None and str(version) != profile.version:
        ctx.warn(
            f"assessment for profile {profile_id!r} was run with version {version}, but the "
            f"installed profile is {profile.version}; criterion descriptions come from the latter"
        )
    return profile


# -- evidence --------------------------------------------------------------------


@dataclass
class _FileUse:
    claims: list[tuple[str, str]] = field(default_factory=list)
    details: list[str] = field(default_factory=list)


def _manifest_path(source: Any, files: dict[str, Any]) -> str | None:
    if not isinstance(source, str) or not source:
        return None
    if source in files:
        return source
    head = source.split("#", 1)[0]
    return head if head in files else None


def _paths_in(value: Any, files: dict[str, Any], depth: int = 0) -> list[str]:
    """Manifest paths named anywhere inside an evidence ``result``. Exact matches only."""
    if depth > 6:
        return []
    if isinstance(value, str):
        return [value] if value in files else []
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in files:
                found.append(key)
            found.extend(_paths_in(item, files, depth + 1))
    elif isinstance(value, list):
        for item in value:
            found.extend(_paths_in(item, files, depth + 1))
    return found


def _collect_evidence(
    ctx: _Context, result: dict[str, Any]
) -> tuple[dict[str, _FileUse], list[str], list[str]]:
    """Files, check ids and claim ids a result cites. Insertion order = citation order."""
    uses: dict[str, _FileUse] = {}
    checks: list[str] = []
    claim_ids: list[str] = []

    def use(path: str) -> _FileUse:
        return uses.setdefault(path, _FileUse())

    def note_check(check: Any) -> None:
        if isinstance(check, str) and check and check not in checks:
            checks.append(check)

    for item in result.get("evidence", []):
        if isinstance(item, str):
            claim = ctx.claims.get(item)
            if claim is None:
                ctx.warn(f"claim {item} cited by {result.get('rule_id')} is not in evidence.json")
                continue
            claim_ids.append(item)
            text = _clip(_squash(claim.get("claim", "")))
            for ledger_item in claim.get("evidence", []):
                note_check(ledger_item.get("check"))
                path = _manifest_path(ledger_item.get("source"), ctx.files)
                if path is None:
                    continue
                pinned = ledger_item.get("source_sha256")
                if pinned and pinned != ctx.files[path].get("sha256"):
                    raise AnnotationExportError(
                        f"claim {item} pins {path} to {pinned}, but the manifest records "
                        f"{ctx.files[path].get('sha256')}; the ingest output is inconsistent"
                    )
                entry = use(path)
                if (item, text) not in entry.claims:
                    entry.claims.append((item, text))
        elif isinstance(item, dict):
            note_check(item.get("check"))
            detail = _clip(_squash(item.get("detail", "")))
            label = f"check {item['check']}" if isinstance(item.get("check"), str) else "a check"
            named = _paths_in(item.get("result"), ctx.files)
            for key in ("path", "file"):
                if isinstance(item.get(key), str) and item[key] in ctx.files:
                    named.append(item[key])
            for path in named:
                sentence = f"{label}: {detail}" if detail else f"{label} cites this file"
                entry = use(path)
                if sentence not in entry.details:
                    entry.details.append(sentence)
    return uses, checks, claim_ids


def _locator_for(ctx: _Context, file_path: str) -> str | None:
    locator = f"{ctx.dataset_path}/{file_path}"
    return locator if _LOCATOR.fullmatch(locator) else None


def _check_file_present(ctx: _Context, file_path: str) -> None:
    if ctx.dataspace is None:
        return
    if file_path not in ctx._present:
        actual = ctx.dataspace / ctx.dataset_path / file_path
        ctx._present[file_path] = actual.is_file()
        if actual.is_file():
            recorded = ctx.files[file_path].get("size")
            if isinstance(recorded, int) and actual.stat().st_size != recorded:
                ctx.warn(
                    f"{ctx.dataset_path}/{file_path} is {actual.stat().st_size} bytes in the "
                    f"dataspace but {recorded} in the ingest manifest (line-ending conversion, or "
                    "a different copy?); the checksum pinned in the annotation is the manifest's"
                )
    if not ctx._present[file_path]:
        raise AnnotationExportError(
            f"evidence file {file_path!r} is in the manifest but {ctx.dataset_path}/{file_path} "
            "does not exist in the dataspace; --dataset-path must be the ingested directory"
        )


def _file_entry(ctx: _Context, file_path: str, description: str) -> dict[str, Any] | None:
    locator = _locator_for(ctx, file_path)
    if locator is None:
        return None
    _check_file_present(ctx, file_path)
    return {
        "locator": locator,
        "checksum": {"algorithm": "sha256", "value": ctx.files[file_path]["sha256"]},
        "description": description,
    }


def _describe_use(rule_id: str, entry: _FileUse) -> str:
    parts = []
    if entry.claims:
        shown = entry.claims[:_MAX_CLAIMS_IN_DESCRIPTION]
        parts.append(
            "examined by rule "
            + rule_id
            + "; cited claim(s): "
            + "; ".join(f"{cid} ({text})" for cid, text in shown)
            + (
                f"; and {len(entry.claims) - len(shown)} more"
                if len(entry.claims) > len(shown)
                else ""
            )
        )
    parts.extend(entry.details)
    if not parts:
        parts.append(f"examined by rule {rule_id}")
    text = "Inventoried file " + "; ".join(parts) + ". Its bytes are pinned by the checksum."
    return text


def _evidence_list(
    ctx: _Context,
    rule_id: str,
    uses: dict[str, _FileUse],
    *,
    lead_files: list[tuple[str, str]] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """Evidence entries plus the manifest paths that became legal entries."""
    entries: list[dict[str, Any]] = []
    cited: list[str] = []
    illegal: list[str] = []
    ordered: list[tuple[str, str]] = list(lead_files)
    ordered += [(path, _describe_use(rule_id, use)) for path, use in uses.items()]
    for path, description in ordered:
        if path in cited or path in illegal:
            continue
        entry = _file_entry(ctx, path, description)
        if entry is None:
            illegal.append(path)
        else:
            entries.append(entry)
            cited.append(path)
    if illegal or not entries:
        if illegal:
            listing = ", ".join(
                f"{name!r} (sha256 {ctx.files[name]['sha256']})" for name in illegal
            )
            description = (
                f"Whole dataset directory. Evidence file(s) {listing} cannot be written as a "
                "legal dataspace locator (characters outside A-Z a-z 0-9 . _ / - # [ ] =, or "
                "'..'), so this entry points at the directory instead."
            )
        else:
            description = (
                f"Whole dataset directory. Rule {rule_id} examined dataset-level facts recorded "
                f"in the ingest manifest and evidence ledger of {ctx.dataset_id}; no single file "
                "is cited, so no per-file checksum applies."
            )
        entries.append({"locator": ctx.dataset_path, "description": description})
    return entries, cited


# -- records ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Stamp:
    created_by: str
    created_at: str
    agent_model: str


def _record(
    ctx: _Context,
    stamp: _Stamp,
    *,
    annotation_id: str,
    annotation_type: str,
    target: dict[str, Any],
    fair_dimension: str | None,
    criterion_id: str,
    description: str,
    label: str,
    rationale: str,
    evidence: list[dict[str, Any]],
    notes: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "annotation_id": annotation_id,
        "dataset": {
            "dataset_family_id": ctx.family,
            "variant": ctx.variant,
            "version": ctx.dataset_id,
        },
        "annotation_type": annotation_type,
        "target": target,
    }
    if fair_dimension is not None:
        record["fair_dimension"] = fair_dimension
    record.update(
        {
            "criterion": {"id": criterion_id, "description": description},
            "judgement": {"label": label, "rationale": rationale},
            "evidence": evidence,
            "provenance": {
                "method": "agent_proposal",
                "created_by": stamp.created_by,
                "created_at": stamp.created_at,
                "agent_model": stamp.agent_model,
                "notes": notes,
            },
            "review": {"status": "draft"},
        }
    )
    return record


# -- shared ids ------------------------------------------------------------------
#
# Agreed with the dataspace owner so that these records and those of other harnesses
# (an LLM harness writes ``phenotyper-original-R1.1-license``, criterion ``FAIR-R1.1``)
# can be compared in one corpus. The Data2Agent rule id is not part of the convention;
# it stays in ``provenance.notes``.

#: Criterion family per record source.
CRITERION_FAMILY: dict[str, str] = {"fair": "FAIR", "quality": "QC", "curation": "CV"}

#: Spelling of the shared corpus, applied word by word to the *exported* slug only. The
#: rule id keeps its own spelling: ``R1.1-LICENCE-DECLARED`` is cited by the profile,
#: the tests and assessments already on disk, and renaming it would orphan them.
EXPORT_SPELLING: dict[str, str] = {"LICENCE": "LICENSE"}


def shared_ids(source: str, rule_id: str, principle: str | None = None) -> tuple[str, str]:
    """``(criterion.id, annotation_id tail)`` under the shared convention.

    ``criterion.id`` is ``<FAMILY>-<code>_<SLUG>``, or ``<FAMILY>_<SLUG>`` where there is
    no code; the tail is ``<code or FAMILY>-<slug in lower case>``:

    * ``fair``: the code is the principle. A rule id ``<principle>-<SLUG>`` is split on
      its *first* hyphen only (``R1.1`` has dots, never hyphens): ``R1.1-LICENCE-DECLARED``
      -> ``FAIR-R1.1_LICENSE-DECLARED`` / ``R1.1-license-declared``. A rule id that does
      not start with its own principle keeps the whole id as its slug, so nothing is lost.
    * ``quality``: no code (a quality rule has a category, not a principle); a leading
      ``Q-`` is dropped: ``Q-HEADERS-PORTABLE`` -> ``QC_HEADERS-PORTABLE`` /
      ``QC-headers-portable``.
    * ``curation``: no code: ``ORIGINAL-UNCHANGED`` -> ``CV_ORIGINAL-UNCHANGED``.
    """
    family = CRITERION_FAMILY[source]
    head, hyphen, rest = rule_id.partition("-")
    code: str | None = None
    slug = rule_id
    if source == "fair":
        code = principle
        if hyphen and rest and head == principle:
            slug = rest
    elif source == "quality" and hyphen and rest and head == "Q":
        slug = rest
    slug = "-".join(EXPORT_SPELLING.get(word, word) for word in slug.split("-"))
    criterion = f"{family}-{code}_{slug}" if code else f"{family}_{slug}"
    return criterion, f"{code or family}-{slug.lower()}"


def _annotation_id(ctx: _Context, tail: str, origin: str, suffix: int | None = None) -> str:
    """``<family>-<variant>-<tail>[-<n>]``. ``origin`` is the rule or check id, for errors."""
    identifier = f"{ctx.family}-{ctx.variant}-{tail}"
    if suffix is not None:
        identifier = f"{identifier}-{suffix}"
    if not _ID_FRAGMENT.fullmatch(tail) or not _ANNOTATION_ID.fullmatch(identifier):
        raise AnnotationExportError(
            f"{origin!r} cannot form a legal annotation_id (allowed: A-Z a-z 0-9 . _ -); "
            "ids are never rewritten beyond the shared convention, so this rule or check "
            "cannot be exported"
        )
    return identifier


def _rationale(rule_id: str, result: dict[str, Any], label: str, checks: list[str]) -> str:
    stated = _squash(result.get("rationale", ""))
    if stated:
        return stated
    cited = ", ".join(checks) if checks else "no named check"
    return (
        f"Rule {rule_id} returned '{result['result']}' ({label}); the deterministic check "
        f"cited these evidence checks: {cited}."
    )


def _parse_offender(ctx: _Context, offender: Any) -> tuple[str | None, str | None] | None:
    """(file, field) for one offender, or None when it cannot be read without guessing."""
    if isinstance(offender, str) and offender.strip():
        return (offender, None) if offender in ctx.files else (None, offender)
    if isinstance(offender, dict):
        file_path = next(
            (
                offender[k]
                for k in ("file", "path", "table", "source")
                if isinstance(offender.get(k), str)
            ),
            None,
        )
        field_name = next(
            (
                offender[k]
                for k in ("field", "field_name", "column", "header")
                if isinstance(offender.get(k), str) and offender[k]
            ),
            None,
        )
        if file_path is not None and file_path not in ctx.files:
            return None
        if file_path is None and field_name is None:
            return None
        return file_path, field_name
    return None


def _quality_targets(
    ctx: _Context, result: dict[str, Any], cited: list[str]
) -> list[tuple[dict[str, Any], str | None]]:
    """One (target, offender-file) per offender; a single dataset target when there are none."""
    dataset_target: dict[str, Any] = {"kind": "dataset", "path": ctx.dataset_path}
    observations = result.get("observations")
    offenders = observations.get("offenders") if isinstance(observations, dict) else None
    if offenders is None or offenders == []:
        return [(dataset_target, None)]
    rule_id = result.get("rule_id")
    if not isinstance(offenders, list):
        ctx.warn(f"{rule_id}: observations.offenders is not a list; reported at dataset level")
        return [(dataset_target, None)]

    targets: list[tuple[dict[str, Any], str | None]] = []
    seen: set[tuple[str | None, str | None]] = set()
    for offender in offenders:
        parsed = _parse_offender(ctx, offender)
        if parsed is None:
            ctx.warn(
                f"{rule_id}: offender {offender!r} skipped: not a manifest file or a field name"
            )
            continue
        if parsed in seen:
            continue
        seen.add(parsed)
        file_path, field_name = parsed
        if file_path is not None:
            locator = _locator_for(ctx, file_path)
            if locator is None:
                ctx.warn(
                    f"{rule_id}: {file_path!r} is not a legal locator; offender reported "
                    "against the dataset directory"
                )
            path = locator or ctx.dataset_path
        else:
            only = cited[0] if len(cited) == 1 else None
            path = (_locator_for(ctx, only) if only else None) or ctx.dataset_path
        if field_name is not None:
            target = {"kind": "field", "path": path, "field_name": field_name}
        else:
            target = {"kind": "file" if path != ctx.dataset_path else "dataset", "path": path}
        targets.append((target, file_path))
    if not targets:
        ctx.warn(f"{rule_id}: no offender could be read; reported at dataset level")
        return [(dataset_target, None)]
    return targets


@dataclass
class BuildResult:
    records: list[dict[str, Any]]
    warnings: list[str]
    skipped: list[dict[str, str]]
    profiles: list[dict[str, Any]]
    dataset_id: str


def _assessment_records(
    ctx: _Context, stamp: _Stamp, assessment: dict[str, Any], profile, skipped: list[dict[str, str]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    profile_id = assessment["profile"]["id"]
    is_quality = profile_id == QUALITY_PROFILE
    records: list[dict[str, Any]] = []
    for result in assessment["results"]:
        rule_id = result.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id:
            raise AnnotationExportError(f"{profile_id}: a result has no rule_id")
        outcome = result.get("result")
        label = label_for_result(outcome)
        try:
            rule = profile.rule(rule_id)
        except KeyError as error:
            raise AnnotationExportError(
                f"assessment cites rule {rule_id!r} that profile {profile_id!r} does not define; "
                "the criterion description is never guessed"
            ) from error

        if is_quality:
            annotation_type, dimension = "quality_issue", None
            if outcome not in _QUALITY_REPORTED:
                skipped.append(
                    {
                        "profile": profile_id,
                        "rule_id": rule_id,
                        "reason": f"quality_issue reports issues only; result was '{outcome}'",
                    }
                )
                continue
        else:
            principle = str(result.get("principle", ""))
            match = _FAIR_PRINCIPLE.fullmatch(principle)
            if match is None:
                ctx.warn(
                    f"{profile_id}/{rule_id}: principle {principle!r} maps to no FAIR dimension "
                    "and the profile is not the quality profile; rule skipped, not guessed"
                )
                skipped.append(
                    {
                        "profile": profile_id,
                        "rule_id": rule_id,
                        "reason": f"principle {principle!r} maps to no FAIR dimension",
                    }
                )
                continue
            annotation_type, dimension = "fair_assessment", FAIR_DIMENSIONS[match.group(1)]
        criterion_id, tail = shared_ids(
            "quality" if is_quality else "fair", rule_id, None if is_quality else principle
        )

        uses, checks, claim_ids = _collect_evidence(ctx, result)
        cited = list(uses)
        rationale = _rationale(rule_id, result, label, checks)
        notes = (
            f"Verdict computed by code, not by a model: Data2Agent profile {profile_id} "
            f"{assessment['profile'].get('version', '?')}, rule {rule_id}, result '{outcome}', "
            f"inferred={bool(result.get('inferred', False))}. "
            f"Data2Agent dataset_id {ctx.dataset_id}."
        )
        if is_quality:
            targets = _quality_targets(ctx, result, cited)
        else:
            targets = [({"kind": "dataset", "path": ctx.dataset_path}, None)]
        numbered = len(targets) > 1
        for index, (target, offender_file) in enumerate(targets, start=1):
            lead = []
            if offender_file is not None:
                subject = f"field {target['field_name']!r}" if "field_name" in target else "content"
                lead = [
                    (
                        offender_file,
                        f"Inventoried file holding the offending {subject} reported by "
                        f"rule {rule_id}. Its bytes are pinned by the checksum.",
                    )
                ]
            evidence, _ = _evidence_list(ctx, rule_id, uses, lead_files=lead)
            records.append(
                _record(
                    ctx,
                    stamp,
                    annotation_id=_annotation_id(ctx, tail, rule_id, index if numbered else None),
                    annotation_type=annotation_type,
                    target=target,
                    fair_dimension=dimension,
                    criterion_id=criterion_id,
                    description=_squash(rule.question),
                    label=label,
                    rationale=rationale,
                    evidence=evidence,
                    notes=notes,
                )
            )
    summary = {
        "id": profile_id,
        "version": assessment["profile"].get("version"),
        "results": len(assessment["results"]),
        "exported": len(records),
    }
    return records, summary


# -- curation log ----------------------------------------------------------------


def _load_curation_log(path: Path, ctx: _Context) -> dict[str, Any]:
    log = _read_json(path, "curation log")
    if not isinstance(log, dict) or str(log.get("log_version")) != "1":
        raise AnnotationExportError(f"{path} is not a curation log with log_version '1'")
    verification = log.get("verification")
    result = log.get("result")
    derived = log.get("derived_from")
    if not (
        isinstance(verification, dict) and isinstance(result, dict) and isinstance(derived, dict)
    ):
        raise AnnotationExportError(
            f"{path}: 'verification', 'result' and 'derived_from' are required"
        )
    if not isinstance(verification.get("original_unchanged"), bool):
        raise AnnotationExportError(
            f"{path}: verification.original_unchanged must be true or false"
        )
    for key in ("cells_compared", "cells_round_trip"):
        value = verification.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AnnotationExportError(
                f"{path}: verification.{key} must be a non-negative integer"
            )
    checks = verification.get("checks", [])
    if not isinstance(checks, list):
        raise AnnotationExportError(f"{path}: verification.checks must be a list")
    for check in checks:
        if not (
            isinstance(check, dict)
            and isinstance(check.get("id"), str)
            and check["id"]
            and check.get("result") in ("pass", "fail")
        ):
            raise AnnotationExportError(
                f"{path}: every verification check needs a string id and result 'pass' or 'fail'"
            )
    if result.get("dataset_id") != ctx.dataset_id:
        raise AnnotationExportError(
            f"{path} describes curated dataset {result.get('dataset_id')!r}, but the ingest output "
            f"is {ctx.dataset_id!r}; ingest the curated dataset the log produced"
        )
    for item in result.get("files", []):
        recorded = ctx.files.get(item.get("path")) if isinstance(item, dict) else None
        if recorded is None or recorded.get("sha256") != item.get("sha256"):
            raise AnnotationExportError(
                f"{path}: result file {item!r} does not match the ingest manifest; the curated "
                "bytes changed after the log was written"
            )
    return log


def _curation_records(
    ctx: _Context, stamp: _Stamp, log: dict[str, Any], log_name: str
) -> list[dict[str, Any]]:
    if ctx.variant != "curated":
        raise ExportRefused("--curation-log describes a curated dataset; use --variant curated")
    verification = log["verification"]
    derived = log["derived_from"]
    result_files = [item["path"] for item in log["result"].get("files", [])]
    uses = {
        path: _FileUse(details=["curated file recorded by the curation log"])
        for path in result_files
    }
    notes_base = (
        f"Recorded by code in the curation log ({log_name}, log_version 1, dated "
        f"{log.get('date', 'unstated')}, by {log.get('by', 'unstated')}); not judged by a model. "
        f"Data2Agent dataset_id {ctx.dataset_id}."
    )
    derived_files = derived.get("files", [])
    derived_text = (
        "derived from "
        + str(derived.get("dataset_id", "an unstated original"))
        + (f" ({len(derived_files)} file(s))" if derived_files else "")
    )

    def make(check_id: str, description: str, passed: bool | None, rationale: str, note: str):
        label = (
            "insufficient_evidence"
            if passed is None
            else ("conformant" if passed else "non_conformant")
        )
        criterion_id, tail = shared_ids("curation", check_id)
        evidence, _ = _evidence_list(ctx, criterion_id, uses)
        return _record(
            ctx,
            stamp,
            annotation_id=_annotation_id(ctx, tail, check_id),
            annotation_type="curation_validation",
            target={"kind": "dataset", "path": ctx.dataset_path},
            fair_dimension=None,
            criterion_id=criterion_id,
            description=description,
            label=label,
            rationale=rationale,
            evidence=evidence,
            notes=f"{note} {notes_base}",
        )

    unchanged = verification["original_unchanged"]
    compared, round_trip = verification["cells_compared"], verification["cells_round_trip"]
    records = [
        make(
            "ORIGINAL-UNCHANGED",
            "The original dataset must be byte-identical before and after curation.",
            unchanged,
            (
                f"The curation log records the original ({derived_text}) as unchanged by curation."
                if unchanged
                else f"The curation log records the original ({derived_text}) as CHANGED."
            ),
            "verification.original_unchanged.",
        ),
        make(
            "CELL-ROUND-TRIP",
            "Every curated cell must map back to its original cell.",
            None if compared == 0 else round_trip == compared,
            (
                "The curation log compared no cells, so round-tripping is unverified."
                if compared == 0
                else f"{round_trip} of {compared} cells round-trip through the documented mapping."
            ),
            "verification.cells_round_trip / cells_compared.",
        ),
    ]
    for check in verification.get("checks", []):
        detail = _squash(check.get("detail", ""))
        records.append(
            make(
                check["id"],
                f"Curation check '{check['id']}' recorded in the curation log.",
                check["result"] == "pass",
                detail or f"The curation log records check '{check['id']}' as {check['result']}.",
                f"verification.checks[{check['id']}].",
            )
        )
    return records


# -- public: build -----------------------------------------------------------------


def normalise_created_at(value: str | None) -> str:
    """``--created-at`` if it is ISO-8601 UTC with a Z, else the current UTC time."""
    if value is None:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not _TIMESTAMP.fullmatch(value):
        raise AnnotationExportError(
            f"--created-at {value!r} must be ISO-8601 UTC ending in Z, e.g. 2026-09-30T09:40:08Z"
        )
    try:
        datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        raise AnnotationExportError(f"--created-at {value!r} is not a real date-time") from None
    return value


def build_annotations(
    ingest_dir: Path | str,
    *,
    family: str,
    variant: str,
    dataset_path: str,
    agent_model: str,
    created_at: str,
    created_by: str | None = None,
    curation_log: Path | str | None = None,
    dataspace: Path | None = None,
    profiles_root: Path | None = None,
) -> BuildResult:
    """Build (but do not write) the proposal records. ``dataset_path`` must already be validated.

    ``profiles_root`` loads rule questions from another profiles directory (tests, or a
    profile that is not shipped); by default the installed profiles are used.
    """
    if not isinstance(agent_model, str) or not agent_model.strip():
        raise AnnotationExportError(
            "agent_model is required: the dataspace demands it on every agent proposal and it "
            "is never defaulted or guessed"
        )
    # The family now leads the annotation_id, so it must also start with a letter or digit.
    if not _ANNOTATION_ID.fullmatch(family or ""):
        raise AnnotationExportError(
            f"family {family!r} cannot form a legal annotation_id "
            "(A-Z a-z 0-9 . _ -, starting with a letter or digit)"
        )
    if variant not in ("original", "curated"):
        raise AnnotationExportError(f"variant must be 'original' or 'curated', not {variant!r}")
    created_at = normalise_created_at(created_at)
    ingest_path = Path(ingest_dir)
    if not ingest_path.is_dir():
        raise AnnotationExportError(f"ingest output directory {ingest_path} does not exist")

    ctx = _load_context(ingest_path, family, variant, dataset_path, dataspace)
    stamp = _Stamp(
        created_by=(
            created_by.strip() if created_by and created_by.strip() else f"data2agent {__version__}"
        ),
        created_at=created_at,
        agent_model=agent_model.strip(),
    )
    assessments = _load_assessments(ingest_path, ctx.dataset_id)
    if not assessments and curation_log is None:
        raise AnnotationExportError(
            f"no assessment*.json in {ingest_path}; run 'data2agent assess' first"
        )

    records: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    profiles: list[dict[str, Any]] = []
    for assessment in assessments:
        profile = _load_profile(
            assessment["profile"]["id"], assessment["profile"].get("version"), profiles_root, ctx
        )
        produced, summary = _assessment_records(ctx, stamp, assessment, profile, skipped)
        records.extend(produced)
        profiles.append(summary)
    if curation_log is not None:
        log_path = Path(curation_log)
        log = _load_curation_log(log_path, ctx)
        produced = _curation_records(ctx, stamp, log, log_path.name)
        records.extend(produced)
        profiles.append(
            {
                "id": "curation-log",
                "version": "1",
                "results": len(produced),
                "exported": len(produced),
            }
        )

    seen: dict[str, int] = {}
    for record in records:
        seen[record["annotation_id"]] = seen.get(record["annotation_id"], 0) + 1
    clashes = sorted(name for name, count in seen.items() if count > 1)
    if clashes:
        raise AnnotationExportError(f"annotation_id collision(s): {clashes}")
    records.sort(key=lambda record: record["annotation_id"])
    return BuildResult(records, list(ctx.warnings), skipped, profiles, ctx.dataset_id)


# -- validation ------------------------------------------------------------------


def assert_proposal_invariants(record: dict[str, Any]) -> None:
    """Hard checks every produced record must pass, with or without the dataspace schema.

    Failing here is a bug in this module, not a property of the data: agent output must be
    a draft proposal. Nothing is corrected or downgraded.
    """
    ident = record.get("annotation_id", "<no annotation_id>") if isinstance(record, dict) else "<?>"

    def bad(message: str) -> ExportInternalError:
        return ExportInternalError(f"internal error, record {ident}: {message}")

    if not isinstance(record, dict):
        raise bad("not an object")
    provenance = record.get("provenance") or {}
    review = record.get("review") or {}
    if provenance.get("method") != "agent_proposal":
        raise bad(f"provenance.method is {provenance.get('method')!r}, must be 'agent_proposal'")
    if review.get("status") != "draft":
        raise bad(f"review.status is {review.get('status')!r}, must be 'draft'")
    for key in ("reviewed_by", "reviewed_at"):
        if key in review:
            raise bad(f"review.{key} must be absent on an agent proposal")
    if not str(provenance.get("agent_model", "")).strip():
        raise bad("provenance.agent_model is required on an agent proposal")
    if not _TIMESTAMP.fullmatch(str(provenance.get("created_at", ""))):
        raise bad("provenance.created_at is not ISO-8601 UTC")
    if not str(provenance.get("created_by", "")).strip():
        raise bad("provenance.created_by is empty")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise bad(f"schema_version must be {SCHEMA_VERSION}")
    if not _ANNOTATION_ID.fullmatch(str(record.get("annotation_id", ""))):
        raise bad("annotation_id is not schema-legal")
    dataset = record.get("dataset") or {}
    if not dataset.get("dataset_family_id") or dataset.get("variant") not in (
        "original",
        "curated",
    ):
        raise bad("dataset needs dataset_family_id and variant original|curated")
    if record.get("annotation_type") not in _TYPES:
        raise bad(f"annotation_type {record.get('annotation_type')!r} is not in the enum")
    target = record.get("target") or {}
    if target.get("kind") not in _KINDS or not _LOCATOR.fullmatch(str(target.get("path", ""))):
        raise bad(f"target {target!r} is not a legal kind/path")
    if record["annotation_type"] == "fair_assessment" and record.get("fair_dimension") not in (
        FAIR_DIMENSIONS.values()
    ):
        raise bad("fair_assessment requires a fair_dimension")
    if record["annotation_type"] != "fair_assessment" and "fair_dimension" in record:
        raise bad("fair_dimension is only for fair_assessment records")
    criterion = record.get("criterion") or {}
    if not criterion.get("id") or not criterion.get("description"):
        raise bad("criterion needs a non-empty id and description")
    judgement = record.get("judgement") or {}
    if judgement.get("label") not in _LABELS:
        raise bad(f"judgement.label {judgement.get('label')!r} is not in the enum")
    if not str(judgement.get("rationale", "")).strip():
        raise bad("judgement.rationale is empty")
    evidence = record.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise bad("at least one evidence entry is required")
    for item in evidence:
        if not _LOCATOR.fullmatch(str(item.get("locator", ""))):
            raise bad(f"evidence locator {item.get('locator')!r} is not a legal locator")
        if not str(item.get("description", "")).strip():
            raise bad("evidence description is empty")
        checksum = item.get("checksum")
        if checksum is not None and not (
            checksum.get("algorithm") == "sha256" and _SHA256.fullmatch(str(checksum.get("value")))
        ):
            raise bad("evidence checksum must be sha256 with 64 lowercase hex digits")


def load_schema(dataspace: Path) -> tuple[dict[str, Any] | None, str]:
    """The dataspace's own schema and a note, or (None, why not)."""
    path = dataspace.joinpath(*SCHEMA_RELATIVE_PATH)
    if not path.is_file():
        return None, f"{'/'.join(SCHEMA_RELATIVE_PATH)} not found in the dataspace"
    return _read_json(path, "annotation schema"), ""


def validate_records(records: list[dict[str, Any]], dataspace: Path) -> tuple[bool, str]:
    """Invariant-check every record, then schema-validate all of them when possible.

    Returns ``(schema_validated, note)``. Any failure raises and nothing has been written.
    """
    for record in records:
        assert_proposal_invariants(record)
    schema, note = load_schema(dataspace)
    if schema is None:
        return False, note
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
    except ImportError:
        return False, "the 'jsonschema' package is not installed (dev dependency)"
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise AnnotationExportError(
            f"the dataspace annotation schema is itself invalid: {error.message}"
        ) from None
    validator = Draft202012Validator(schema)
    problems = []
    for record in records:
        for error in sorted(validator.iter_errors(record), key=lambda e: list(map(str, e.path))):
            where = "/".join(str(part) for part in error.path) or "<record>"
            problems.append(f"{record.get('annotation_id')}: {where}: {error.message}")
    if problems:
        shown = "\n  ".join(problems[:20])
        more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        raise AnnotationExportError(
            f"{len(problems)} schema error(s); nothing was written:\n  {shown}{more}"
        )
    return True, ""


# -- writing ---------------------------------------------------------------------


def _existing_is_untouched_draft(path: Path) -> bool:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        return (
            existing["provenance"]["method"] == "agent_proposal"
            and existing["review"]["status"] == "draft"
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def serialise(record: dict[str, Any]) -> str:
    return json.dumps(record, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def write_records(
    records: list[dict[str, Any]], output_dir: Path, dataspace: Path, *, overwrite: bool = False
) -> list[Path]:
    """Write validated records into ``annotations/proposed/`` -- all of them or none.

    Refuses any output directory other than exactly ``<dataspace>/annotations/proposed``,
    any existing file unless ``overwrite`` (and even then any file a reviewer has moved
    past ``draft``), and stages every file before the first one becomes visible.
    """
    _require_exact_output_dir(output_dir, dataspace)
    resolved_dir = output_dir.resolve()
    targets: list[tuple[Path, str]] = []
    for record in records:
        assert_proposal_invariants(record)
        path = output_dir / f"{record['annotation_id']}.json"
        if path.parent.resolve() != resolved_dir:
            raise ExportRefused(f"{path} would fall outside {resolved_dir}")
        targets.append((path, serialise(record)))

    existing = [path for path, _ in targets if path.exists() or path.is_symlink()]
    if existing and not overwrite:
        names = ", ".join(path.name for path in existing[:10])
        more = f" (+{len(existing) - 10} more)" if len(existing) > 10 else ""
        raise ExportRefused(
            f"{len(existing)} annotation file(s) already exist: {names}{more}; "
            "pass --overwrite to replace them. Nothing was written."
        )
    reviewed = [
        path.name
        for path in existing
        if path.is_symlink() or not _existing_is_untouched_draft(path)
    ]
    if reviewed:
        raise ExportRefused(
            f"refusing to overwrite {', '.join(reviewed[:10])}: not an untouched draft proposal "
            "(reviewed, edited by a human, unreadable, or not a regular file). Nothing was written."
        )

    previous = {path: path.read_bytes() for path in existing}
    staged: list[tuple[Path, Path]] = []
    replaced: list[Path] = []
    try:
        for path, text in targets:
            temporary = path.with_name(f".{path.name}.tmp")
            temporary.write_text(text, encoding="utf-8", newline="\n")
            staged.append((temporary, path))
        for temporary, path in staged:
            os.replace(temporary, path)
            replaced.append(path)
    except BaseException:
        # Best-effort rollback so a failure part-way leaves the folder as it was found.
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
        for path in replaced:
            if path in previous:
                path.write_bytes(previous[path])
            else:
                path.unlink(missing_ok=True)
        raise
    return [path for _, path in staged]


# -- public: export --------------------------------------------------------------


def export_annotations(
    ingest_dir: Path | str,
    *,
    dataspace: Path | str,
    dataset_path: str,
    family: str,
    variant: str,
    agent_model: str,
    created_by: str | None = None,
    created_at: str | None = None,
    curation_log: Path | str | None = None,
    overwrite: bool = False,
    profiles_root: Path | None = None,
) -> dict[str, Any]:
    """Export an ingest output's assessments as proposed annotations. Returns the summary.

    Order matters: validate paths, build every record, validate every record, and only then
    write -- so a failure at any earlier step leaves the dataspace untouched.
    """
    root = resolve_dataspace(dataspace)
    relative = validate_dataset_path(root, dataset_path)
    output_dir = proposed_dir(root)
    built = build_annotations(
        ingest_dir,
        family=family,
        variant=variant,
        dataset_path=relative,
        agent_model=agent_model,
        created_at=normalise_created_at(created_at),
        created_by=created_by,
        curation_log=curation_log,
        dataspace=root,
        profiles_root=profiles_root,
    )
    if not built.records:
        raise AnnotationExportError(
            "nothing to export: every result was skipped "
            f"({len(built.skipped)} skipped); see the warnings"
        )
    validated, note = validate_records(built.records, root)
    warnings = list(built.warnings)
    if not validated:
        warnings.append(f"records were NOT validated against the dataspace schema: {note}")
    written = write_records(built.records, output_dir, root, overwrite=overwrite)

    by_type: dict[str, int] = {}
    for record in built.records:
        by_type[record["annotation_type"]] = by_type.get(record["annotation_type"], 0) + 1
    return {
        "dataset_id": built.dataset_id,
        "dataset_family_id": family,
        "variant": variant,
        "dataset_path": relative,
        "output_dir": "/".join(PROPOSED_SUBPATH),
        "written": len(written),
        "by_type": dict(sorted(by_type.items())),
        "files": [path.name for path in written],
        "profiles": built.profiles,
        "skipped": built.skipped,
        "warnings": warnings,
        "schema_validated": validated,
        **({"schema_validation_note": note} if not validated else {}),
    }
