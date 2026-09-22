"""The host-agnostic dataset service behind Data2MCP.

Every method that returns file content first re-checksums the file against the
manifest. If the bytes have moved, the service says so instead of answering: an
answer drawn from bytes that no longer match the dataset identity is worse than
no answer, because it looks exactly like a good one.

This module imports nothing from ``mcp`` and nothing host-specific, so it is
directly testable and directly reusable by any harness.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import ModeError, OutputError
from ..evidence import EvidenceLedger
from ..ingest.checksum import hash_file
from ..ingest.pipeline import EVIDENCE_FILENAME, MANIFEST_FILENAME, PROVENANCE_FILENAME
from .modes import ALL_RESOURCES, DEFAULT_MODE, Mode, resolve_mode

# Content is served in bounded slices; an agent that wants more asks again.
_DEFAULT_PREVIEW_BYTES = 4096
_MAX_PREVIEW_BYTES = 262_144


@dataclass
class IntegrityStatus:
    """Whether a file on disk still matches what the manifest recorded."""

    path: str
    expected_sha256: str
    observed_sha256: str | None
    matches: bool
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "expected_sha256": self.expected_sha256,
            "observed_sha256": self.observed_sha256,
            "matches": self.matches,
            **({"detail": self.detail} if self.detail else {}),
        }


class DatasetService:
    """Read-only access to one ingested dataset."""

    def __init__(
        self,
        output_dir: Path,
        *,
        source_dir: Path | None = None,
        mode: str = DEFAULT_MODE,
    ) -> None:
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.mode: Mode = resolve_mode(mode)

        self.manifest = _load_json(self.output_dir / MANIFEST_FILENAME)
        self.provenance = _load_json(self.output_dir / PROVENANCE_FILENAME)
        evidence = _load_json(self.output_dir / EVIDENCE_FILENAME)
        _require_one_dataset(self.manifest, self.provenance, evidence)
        self.ledger = EvidenceLedger.from_dict(evidence)

        recorded_source = self.provenance.get("source_path")
        candidate = (
            Path(source_dir) if source_dir else (Path(recorded_source) if recorded_source else None)
        )
        if candidate is None:
            raise OutputError(
                f"{PROVENANCE_FILENAME} does not record a source path; pass source_dir explicitly"
            )
        self.source_dir = candidate.expanduser().resolve()
        self._by_path = {entry["path"]: entry for entry in self.manifest.get("files", [])}
        self._profiles: dict[str, Any] = {}

    # -- capability surface -------------------------------------------------

    @property
    def dataset_id(self) -> str:
        return self.manifest.get("dataset_id", "")

    @property
    def ingested_at(self) -> str | None:
        """When the ingest that produced this output began reading the bytes."""
        return self.provenance.get("started_at")

    def available_tools(self) -> list[str]:
        return list(self.mode.tools)

    def available_resources(self) -> list[str]:
        return list(self.mode.resources)

    def supports(self, tool: str) -> bool:
        return tool in self.mode.tools

    def serves(self, uri: str) -> bool:
        """Whether this mode may serve a resource URI, templates included."""
        if uri in self.mode.resources:
            return True
        return (
            uri.startswith("dataset://files/") and "dataset://files/{path}" in self.mode.resources
        )

    # -- tools --------------------------------------------------------------

    def dataset_inventory(self) -> dict[str, Any]:
        """Summarise the dataset without reading any file content."""
        return {
            "dataset_id": self.dataset_id,
            "manifest_version": self.manifest.get("manifest_version"),
            "root_name": self.manifest.get("source", {}).get("root_name"),
            # Run-specific, so it lives in provenance.json rather than the
            # manifest -- but surfaced here, because "when was this ingested?"
            # is the first thing anyone asks.
            "ingested_at": self.ingested_at,
            "ingest_duration_s": self.provenance.get("duration_seconds"),
            "tool_version": self.provenance.get("tool", {}).get("version"),
            "missing_value_convention": self.manifest.get("missing_value_convention", {}),
            "file_count": self.manifest.get("file_count", 0),
            "total_bytes": self.manifest.get("total_bytes", 0),
            "formats": self.manifest.get("formats", {}),
            "table_count": len(self.manifest.get("tables", {})),
            "metadata_files": self.manifest.get("metadata_files", []),
            "identifier_count": len(self.manifest.get("identifiers", [])),
            # Empty means "this version does not determine relationships", not
            # "this dataset has none". The distinction matters for FAIR scoring.
            "relationships": self.manifest.get("relationships", []),
            "relationships_determined": False,
            "warnings": self.manifest.get("warnings", []),
            "skipped": self.manifest.get("skipped", []),
            "mode": self.mode.name,
            "evidence_claims": len(self.ledger),
        }

    def list_files(
        self, *, pattern: str | None = None, file_format: str | None = None
    ) -> list[dict[str, Any]]:
        """List inventoried files, optionally filtered by glob and/or format."""
        results = []
        for entry in self.manifest.get("files", []):
            if pattern and not fnmatch.fnmatch(entry["path"], pattern):
                continue
            if file_format and entry.get("format") != file_format:
                continue
            results.append(entry)
        return results

    def inspect_file(
        self, path: str, *, preview_bytes: int = _DEFAULT_PREVIEW_BYTES
    ) -> dict[str, Any]:
        """Return a file's manifest record, integrity status, and a bounded preview."""
        entry = self._require_entry(path)
        absolute = self._resolve(path)
        integrity = self.verify_file(path)

        payload: dict[str, Any] = {
            "path": entry["path"],
            "size": entry["size"],
            "sha256": entry["sha256"],
            "format": entry.get("format"),
            "media_type": entry.get("media_type"),
            "detected_by": entry.get("detected_by"),
            "integrity": integrity.as_dict(),
        }
        # Present only when the name made a claim, mirroring the manifest. This
        # method promises the file's manifest record, so omitting a recorded
        # field would make the promise false.
        if "extension_format" in entry:
            payload["extension_format"] = entry["extension_format"]
            payload["extension_conflict"] = entry.get("extension_conflict", False)
        if not integrity.matches:
            payload["preview"] = None
            payload["preview_withheld"] = (
                "the file no longer matches its manifest checksum; re-ingest before relying on it"
            )
            return payload

        limit = max(0, min(int(preview_bytes), _MAX_PREVIEW_BYTES))
        # read(limit), not read_bytes()[:limit]: slicing after the fact would pull
        # a multi-gigabyte file entirely into memory to hand back 4 KiB of it, and
        # take the server down with it.
        with absolute.open("rb") as handle:
            raw = handle.read(limit)
        try:
            payload["preview"] = raw.decode("utf-8")
            payload["preview_encoding"] = "utf-8"
        except UnicodeDecodeError:
            payload["preview"] = None
            payload["preview_encoding"] = "binary"
            payload["preview_note"] = "file is not valid UTF-8; no textual preview is offered"
        payload["preview_bytes"] = len(raw)
        payload["preview_truncated"] = len(raw) < entry["size"]
        return payload

    def inspect_table(self, path: str) -> dict[str, Any]:
        """Return the recorded profile of a delimited table."""
        self._require_entry(path)
        profile = self.manifest.get("tables", {}).get(path)
        if profile is None:
            raise KeyError(
                f"'{path}' was not profiled as a table; "
                "call inspect_file for its format and preview"
            )
        return {**profile, "integrity": self.verify_file(path).as_dict()}

    def get_metadata(self, path: str | None = None) -> dict[str, Any]:
        """Serve recognised metadata files verbatim.

        Verbatim is the point: paraphrasing a metadata record here would make the
        downstream claim unverifiable against the bytes.
        """
        recognised = {item["path"]: item for item in self.manifest.get("metadata_files", [])}
        if path is None:
            return {
                "dataset_id": self.dataset_id,
                "metadata_files": list(recognised.values()),
                "note": "call get_metadata(path=...) for a file's verbatim contents",
            }
        if path not in recognised:
            known = ", ".join(sorted(recognised)) or "none recognised"
            raise KeyError(f"'{path}' is not a recognised metadata file; recognised: {known}")

        integrity = self.verify_file(path)
        payload: dict[str, Any] = {**recognised[path], "integrity": integrity.as_dict()}
        if not integrity.matches:
            payload["content"] = None
            payload["content_withheld"] = "file no longer matches its manifest checksum"
            return payload

        absolute = self._resolve(path)
        try:
            payload["content"] = absolute.read_text(encoding="utf-8")
            payload["content_encoding"] = "utf-8"
        except UnicodeDecodeError:
            payload["content"] = None
            payload["content_encoding"] = "binary"
        return payload

    def get_provenance(self) -> dict[str, Any]:
        """Return when, where and with what this dataset was ingested.

        Separate from the manifest by design, and separate from
        ``dataset_inventory`` because provenance answers a different question:
        not what the dataset is, but what this run of the tool was.
        """
        return {
            "dataset_id": self.dataset_id,
            "ingested_at": self.ingested_at,
            "started_at": self.provenance.get("started_at"),
            "finished_at": self.provenance.get("finished_at"),
            "duration_seconds": self.provenance.get("duration_seconds"),
            "tool": self.provenance.get("tool", {}),
            "runtime": self.provenance.get("runtime", {}),
            "configuration": self.provenance.get("configuration", {}),
            "source_verified_unchanged": self.provenance.get("source_verified_unchanged"),
            "source_mutated": self.provenance.get("source_mutated"),
            "note": (
                "timestamps describe this ingest run, not the dataset; manifest.json "
                "is timestamp-free so repeated ingests of the same bytes compare equal"
            ),
        }

    def get_evidence(
        self,
        *,
        claim_id: str | None = None,
        subject: str | None = None,
        check: str | None = None,
        contains: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Query the claim -> evidence ledger."""
        if claim_id:
            record = self.ledger.get(claim_id)
            if record is None:
                raise KeyError(f"no claim with id '{claim_id}'")
            return {"dataset_id": self.dataset_id, "claims": [record.as_dict()], "total": 1}

        matches = self.ledger.query(subject=subject, check=check, contains=contains)
        capped = matches[: max(1, int(limit))]
        return {
            "dataset_id": self.dataset_id,
            "claims": [record.as_dict() for record in capped],
            "total": len(matches),
            "returned": len(capped),
        }

    def resolve_identifier(self, value: str) -> dict[str, Any]:
        """Report where an identifier occurs in the dataset.

        This is occurrence lookup, not resolution: no network call is made, and a
        hit is never evidence that the identifier resolves or is valid. Actual
        resolution needs the network, which no check in this version makes;
        `validate_identifier` in a fair-* mode checks syntax only.
        """
        needle = value.strip().lower()
        occurrences = [
            hit for hit in self.manifest.get("identifiers", []) if hit["value"].lower() == needle
        ]
        return {
            "query": value,
            "occurrences": occurrences,
            "found": bool(occurrences),
            "resolved": None,
            "note": "occurrence lookup only; no resolution was attempted",
        }

    # -- profile tools (fair-* modes only) ----------------------------------

    def profile_context(self):
        """Build the read-only view a profile check is allowed to see.

        Deliberately narrow: the manifest, the evidence ledger, and the text of
        recognised metadata files. A check that could re-read the dataset could
        reach a conclusion the evidence ledger cannot account for.
        """
        from ..profiles.model import ProfileContext

        return ProfileContext(
            manifest=self.manifest,
            ledger=self.ledger,
            read_metadata=self._metadata_text,
        )

    def load_profile(self, profile_id: str = "fair"):
        """Load and cache an assessment profile."""
        from ..profiles.loader import load_profile

        if profile_id not in self._profiles:
            self._profiles[profile_id] = load_profile(profile_id)
        return self._profiles[profile_id]

    def list_fair_rules(self) -> dict[str, Any]:
        """List the canonical FAIR rules: id, principle, question, implementation status."""
        profile = self.load_profile("fair")
        return {
            "profile": {"id": profile.id, "version": profile.version, "title": profile.title},
            "description": profile.description,
            "rules": [
                {
                    "id": rule.id,
                    "principle": rule.principle,
                    "question": rule.question,
                    "check": rule.check_type,
                    "implemented": rule.implemented,
                    "allowed_results": list(rule.allowed_results),
                }
                for rule in profile.rules
            ],
        }

    def get_fair_indicator(self, rule_id: str) -> dict[str, Any]:
        """Return one canonical FAIR rule in full, exactly as authored."""
        return self.load_profile("fair").rule(rule_id).as_dict()

    def run_fair_check(
        self,
        rule_id: str | None = None,
        *,
        host: str | None = None,
        model: str | None = None,
        orchestrator: str | None = None,
    ) -> dict[str, Any]:
        """Run the deterministic FAIR checks and return an assessment.

        The verdicts come from code, not from a model. ``unknown`` results are
        preserved rather than resolved: a rule this version cannot run reports
        unknown and stays in the denominator.
        """
        from ..profiles.fair import CHECKS
        from ..profiles.runner import run

        profile = self.load_profile("fair")
        generator: dict[str, Any] = {
            "mode": self.mode.name,
            "orchestrator": orchestrator or "deterministic",
        }
        if host:
            generator["host"] = host
        if model:
            generator["model"] = model

        assessment = run(
            profile, self.profile_context(), CHECKS, generator=generator, rule_id=rule_id
        )
        payload = assessment.as_dict()
        payload["summary"] = assessment.summary()
        return payload

    def validate_identifier(self, value: str) -> dict[str, Any]:
        """Check an identifier's syntax against its scheme. No network request is made.

        Syntactic validity is not resolution. A well-formed DOI that points at
        nothing still passes here, and the response says so explicitly so the
        distinction cannot be lost downstream.
        """
        from ..ingest.identifiers import validate

        result = validate(value)
        result["resolution_attempted"] = False
        result["resolves"] = None
        result["note"] = (
            "syntax only; whether this identifier resolves was not checked and must not "
            "be inferred from a valid syntax"
        )
        occurrences = self.resolve_identifier(value)
        result["occurrences"] = occurrences["occurrences"]
        return result

    # -- resources ----------------------------------------------------------

    def resource(self, uri: str) -> str:
        """Serve a ``dataset://`` resource as text, subject to the mode's gating.

        Gating lives here rather than only in the MCP binding so that it holds
        for every caller -- a harness driving the service directly is bound by
        the same condition as one going through the protocol.
        """
        # A URI outside the registry does not exist; one inside it may still be
        # withheld by the mode. The two are different answers and different errors.
        if uri not in ALL_RESOURCES and not uri.startswith("dataset://files/"):
            raise KeyError(f"unknown resource uri: {uri}")
        if not self.serves(uri):
            allowed = ", ".join(self.mode.resources) or "none"
            raise ModeError(f"mode '{self.mode.name}' does not serve {uri!r}; it serves: {allowed}")
        if uri == "dataset://manifest":
            return json.dumps(self.manifest, indent=2, ensure_ascii=False)
        if uri == "dataset://provenance":
            return json.dumps(self.provenance, indent=2, ensure_ascii=False)
        if uri == "dataset://evidence":
            return json.dumps(self.ledger.as_dict(), indent=2, ensure_ascii=False)
        if uri == "dataset://metadata":
            return json.dumps(self.get_metadata(), indent=2, ensure_ascii=False)
        if uri.startswith("dataset://files/"):
            return json.dumps(
                self.inspect_file(uri[len("dataset://files/") :]), indent=2, ensure_ascii=False
            )
        raise KeyError(f"unknown resource uri: {uri}")

    # -- integrity ----------------------------------------------------------

    def verify_file(self, path: str) -> IntegrityStatus:
        entry = self._require_entry(path)
        absolute = self._resolve(path)
        if not absolute.is_file():
            return IntegrityStatus(
                path, entry["sha256"], None, False, "file is missing from the source tree"
            )
        observed = hash_file(absolute)
        return IntegrityStatus(path, entry["sha256"], observed, observed == entry["sha256"])

    def verify_dataset(self) -> dict[str, Any]:
        """Re-checksum every file and report drift against the manifest."""
        statuses = [self.verify_file(entry["path"]) for entry in self.manifest.get("files", [])]
        mismatched = [status for status in statuses if not status.matches]
        return {
            "dataset_id": self.dataset_id,
            "files_checked": len(statuses),
            "intact": not mismatched,
            "mismatched": [status.as_dict() for status in mismatched],
        }

    # -- internals ----------------------------------------------------------

    def _require_entry(self, path: str) -> dict[str, Any]:
        entry = self._by_path.get(path)
        if entry is None:
            raise KeyError(f"'{path}' is not in the dataset manifest")
        return entry

    def _resolve(self, path: str) -> Path:
        """Resolve a dataset-relative path, refusing to escape the dataset root."""
        absolute = (self.source_dir / path).resolve()
        if absolute != self.source_dir and self.source_dir not in absolute.parents:
            raise OutputError(f"path escapes the dataset root: {path}")
        return absolute

    def _metadata_text(self, path: str) -> str | None:
        """Read a recognised metadata file as text, or return None."""
        try:
            served = self.get_metadata(path)
        except KeyError:
            return None
        return served.get("content")


def _require_one_dataset(
    manifest: dict[str, Any], provenance: dict[str, Any], evidence: dict[str, Any]
) -> None:
    """Refuse an output directory whose three documents describe different datasets.

    They can disagree if an ingest was interrupted between writes, if a directory
    was partly overwritten by a second run, or if files from two runs were mixed.
    Serving that state would attach one dataset's evidence to another's manifest
    and label it with the manifest's id -- a wrong answer wearing the exact shape
    of a right one, which is the failure this whole design exists to prevent.
    """
    ids = {
        MANIFEST_FILENAME: manifest.get("dataset_id"),
        PROVENANCE_FILENAME: provenance.get("dataset_id"),
        EVIDENCE_FILENAME: evidence.get("dataset_id"),
    }
    if len(set(ids.values())) > 1:
        detail = ", ".join(f"{name}={value!r}" for name, value in sorted(ids.items()))
        raise OutputError(
            "this output directory does not describe a single dataset: "
            f"{detail}. Re-run `data2agent ingest` into a clean directory."
        )


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise OutputError(
            f"missing {path.name}; run `data2agent ingest` first (looked in {path.parent})"
        )
    return json.loads(path.read_text(encoding="utf-8"))
