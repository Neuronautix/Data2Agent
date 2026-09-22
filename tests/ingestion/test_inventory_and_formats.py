"""Inventory, format detection, metadata recognition and identifier extraction."""

from __future__ import annotations

from pathlib import Path

from data2agent.ingest import formats, identifiers, metadata
from data2agent.ingest.inventory import build


def test_inventory_is_sorted_and_relative(example_dataset: Path):
    inventory = build(example_dataset)
    paths = [entry.path for entry in inventory.files]
    assert paths == sorted(paths)
    assert paths == ["README.md", "animals.csv", "dataset_description.json", "observations.csv"]
    assert all(not path.startswith("/") for path in paths)


def test_unknown_formats_stay_unknown(tmp_path: Path):
    path = tmp_path / "instrument.xyzzy"
    path.write_bytes(b"\x00\x01\x02\x03")
    detected, notes = formats.detect(path)
    assert detected.format_id == "unknown"
    assert detected.detected_by == "none"
    assert notes, "an unrecognised format must produce a warning, not silence"


def test_bytes_outrank_a_misleading_extension(tmp_path: Path):
    path = tmp_path / "table.csv"
    path.write_bytes(b"\x89PNG\r\n\x1a\nnot really a csv")
    detected, notes = formats.detect(path)
    assert detected.format_id == "png"
    assert detected.detected_by == "signature"
    # The disagreement is both warned about and recorded structurally, so a
    # consumer of the manifest can find it without parsing prose.
    assert any("content is" in note for note in notes)
    assert detected.extension_format == "csv"
    assert detected.extension_conflict is True


def test_metadata_files_are_recognised_by_convention():
    assert metadata.classify("dataset_description.json").convention == "bids"
    assert metadata.classify("ro-crate-metadata.json").convention == "ro-crate"
    assert metadata.classify("README.md").convention == "readme"
    assert metadata.classify("animals.csv") is None


def test_identifiers_are_detected_with_their_location(example_dataset: Path):
    hits = identifiers.scan_text_file(example_dataset / "README.md", "README.md")
    by_scheme = {hit.scheme: hit for hit in hits}
    assert by_scheme["doi"].value == "10.5281/zenodo.0000000"
    assert by_scheme["orcid"].value == "0000-0002-1825-0097"
    assert by_scheme["rrid"].value == "RRID:IMSR_JAX:000664"
    assert all(hit.line > 0 and hit.source == "README.md" for hit in hits)


def test_excluded_directories_are_reported_not_silently_dropped(dataset_copy: Path):
    (dataset_copy / ".git").mkdir()
    (dataset_copy / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    inventory = build(dataset_copy)
    assert ".git" in inventory.skipped
    assert all(not entry.path.startswith(".git") for entry in inventory.files)


def _minimal_xlsx(path: Path) -> Path:
    """Write a structurally valid, minimal OOXML workbook.

    Built here rather than committed as a binary so the fixture's contents are
    reviewable and its provenance is the test itself.
    """
    import zipfile

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"/>',
        )
        archive.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook/>')
    return path


def test_ooxml_named_xls_is_identified_by_container(tmp_path: Path):
    """The XP14 case: twenty files named .xls whose bytes are OOXML (D2A-46)."""
    detected, notes = formats.detect(_minimal_xlsx(tmp_path / "legacy_name.xls"))

    assert detected.format_id == "xlsx"
    assert detected.detected_by == "container"
    # The name's claim is kept verbatim, never rewritten, and the disagreement
    # is reported rather than silently resolved.
    assert detected.extension_format == "xls"
    assert detected.extension_conflict is True
    assert any("claims 'xls'" in note and "'xlsx'" in note for note in notes)


def test_correctly_named_xlsx_is_verified_not_assumed(tmp_path: Path):
    detected, notes = formats.detect(_minimal_xlsx(tmp_path / "honest.xlsx"))

    assert detected.format_id == "xlsx"
    assert detected.detected_by == "container"  # confirmed from bytes, not trusted
    assert detected.extension_conflict is False
    assert notes == []


def test_generic_zip_is_never_promoted_to_ooxml(tmp_path: Path):
    """A ZIP is not a workbook merely because it starts with PK\x03\x04."""
    import zipfile

    archive_path = tmp_path / "plain.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("notes.txt", "nothing office-like in here")

    detected, _ = formats.detect(archive_path)
    assert detected.format_id == "zip"  # from the extension; not upgraded
    assert detected.extension_conflict is False

    unnamed = tmp_path / "plain.bin"
    unnamed.write_bytes(archive_path.read_bytes())
    assert formats.detect(unnamed)[0].format_id == "zip-container"


def test_truncated_zip_falls_back_to_the_signature(tmp_path: Path):
    """A damaged archive must not raise; the ZIP signature still stands."""
    broken = tmp_path / "truncated.xlsx"
    broken.write_bytes(b"PK\x03\x04" + b"\x00" * 40)

    detected, _ = formats.detect(broken)
    assert detected.format_id in {"zip-container", "xlsx"}
    assert detected.detected_by in {"signature", "extension"}


def test_a_specific_extension_still_beats_a_generic_container(tmp_path: Path):
    """'.rdf' over XML bytes is RDF/XML -- the more specific of two true answers."""
    rdf = tmp_path / "graph.rdf"
    rdf.write_text('<?xml version="1.0"?><rdf:RDF/>', encoding="utf-8")

    detected, notes = formats.detect(rdf)
    assert detected.format_id == "rdfxml"
    assert detected.extension_conflict is False
    assert notes == []


def test_xml_content_resolves_an_unlisted_extension(tmp_path: Path):
    """GraphPad .pzfx is XML; it resolves to its container type, not to a
    GraphPad format this tool cannot parse (D2A-48, folded into D2A-46)."""
    pzfx = tmp_path / "graphs.pzfx"
    pzfx.write_text('<?xml version="1.0"?><GraphPadPrismFile/>', encoding="utf-8")

    detected, _ = formats.detect(pzfx)
    assert detected.format_id == "xml"
    assert detected.detected_by == "signature"
    assert detected.extension_format is None  # the extension made no claim


def test_unknown_still_reachable(tmp_path: Path):
    """Content sniffing must not become a catch-all that always guesses."""
    opaque = tmp_path / "mystery.bin"
    opaque.write_bytes(b"\x00\x01\x02\x03 not a format we know")

    detected, notes = formats.detect(opaque)
    assert detected.format_id == "unknown"
    assert detected.detected_by == "none"
    assert notes


def test_hdf5_backed_formats_are_not_false_conflicts(tmp_path: Path):
    """'.nwb' over HDF5 bytes is the specific name for the general container.

    Reported as a generic 'hdf5' with a spurious conflict before the carrier
    map learned about HDF5-backed formats.
    """
    nwb = tmp_path / "recording.nwb"
    nwb.write_bytes(b"\x89HDF\r\n\x1a\n" + b"\x00" * 32)

    detected, notes = formats.detect(nwb)
    assert detected.format_id == "nwb"
    assert detected.extension_conflict is False
    assert notes == []


def test_the_conflict_reaches_evidence_and_inspection(tmp_path: Path):
    """A conflict recorded only in the manifest cannot be cited by an agent."""
    import json
    import zipfile

    from data2agent.ingest.pipeline import ingest
    from data2agent.mcp.service import DatasetService

    source = tmp_path / "ds"
    source.mkdir()
    with zipfile.ZipFile(source / "legacy.xls", "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")

    out = tmp_path / "out"
    ingest(source, out)

    evidence = json.loads((out / "evidence.json").read_text(encoding="utf-8"))
    results = [
        item["result"]
        for claim in evidence["claims"]
        for item in claim["evidence"]
        if item["check"] == "file.format-detection"
    ]
    assert results and results[0]["extension_conflict"] is True
    assert results[0]["extension_format"] == "xls"

    payload = DatasetService(out, mode="structured").inspect_file("legacy.xls")
    assert payload["extension_conflict"] is True
    assert payload["extension_format"] == "xls"
