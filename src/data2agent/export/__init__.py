"""Export of Data2Agent results into other systems' formats.

This package sits **above** ``profiles`` and ``mcp`` in the layering: it may read what
they produced, and nothing that already exists may import it
(``tests/test_layering.py``). ``annotations`` turns assessments into FAIR-VCG
*proposed* annotations; ``reference_labels`` (a separate module, deliberately not
re-exported here) reads approved answers for scoring tools and must never be
exposed to an agent under evaluation.
"""

from .annotations import (
    AnnotationExportError,
    ExportInternalError,
    ExportRefused,
    build_annotations,
    export_annotations,
    label_for_result,
    result_for_label,
)

__all__ = [
    "AnnotationExportError",
    "ExportInternalError",
    "ExportRefused",
    "build_annotations",
    "export_annotations",
    "label_for_result",
    "result_for_label",
]
