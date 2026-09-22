"""Emit the XP14 gold semantic statements as JSONL.

Per-animal and per-session statements are generated from the frozen snapshot so
they stay in step with the source. Statements whose subject or value the dataset
cannot settle are emitted with status 'unknown' or 'conflicted' and carry no
value -- they are never dropped and never guessed.

Every statement carries `evidence`, a list of {artifact, locator, layer} items.
`layer` distinguishes where the fact was found:
    source_data | source_metadata | protocol_evidence | analysis_output | presentation
so that a fact recovered from a slide is usable without being laundered into
something the spreadsheets claim.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


def _repo_root() -> Path:
    """Repository root, derived from this file's location.

    Keeps the generators runnable on any machine and inside CI. Override with
    the D2A_REPO environment variable if the scripts are vendored elsewhere.
    """
    import os

    env = os.environ.get("D2A_REPO")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[3]


REPO = _repo_root()
GOLD = REPO / "benchmarks" / "xp14_apa" / "gold"

B1 = "230807-11_APA-1_3M-3F-dCdC (By M Marias)"
B2 = "230904-08_APA-2_3M++-6M-dCdC"
REG = "Batch APA-1&2 Results/IDs Batch Sex Group.xlsx"
BWF = f"{B2}/APA_Batch2_BW_IDs.xlsx"
DECK18 = "Batch APA-1&2 Results/230918_ShanK3 APA results hD.pptx"
DECK11 = "Batch APA-1&2 Results/230911_APA_Shank3 dCdC_HD.pptx"

out: list[dict] = []
n = 0


def add(**kw):
    global n
    n += 1
    kw["statement_id"] = f"XP14-ST{n:03d}"
    out.append(kw)


animals = list(csv.DictReader((GOLD / "animals.csv").open(encoding="utf-8")))
sessions = list(csv.DictReader((GOLD / "sessions.csv").open(encoding="utf-8")))

# ---------------------------------------------------------------- animals ---
for a in animals:
    ev_reg = [
        {
            "artifact": REG,
            "locator": a["evidence"].split(";")[0].strip(),
            "layer": "source_metadata",
        }
    ]

    add(
        type="animal_has_sex",
        subject=a["animal_key"],
        predicate="has_sex",
        object=a["sex"],
        status="established",
        extraction="deterministic_mapping",
        llm_required=False,
        evidence=ev_reg,
    )

    geno = {
        "type": "animal_has_genotype",
        "subject": a["animal_key"],
        "predicate": "has_genotype",
        "object": a["genotype"],
        "qualifiers": {"gene": "Shank3"},
        "extraction": "deterministic_mapping",
        "llm_required": False,
        "evidence": list(ev_reg),
    }
    if a["genotype_corroborating_source"]:
        geno["evidence"].append(
            {
                "artifact": BWF,
                "locator": a["evidence"].split(";")[-1].strip(),
                "layer": "source_metadata",
            }
        )
    if a["batch"] == "2":
        geno["status"] = "established"
        geno["caveat"] = (
            "the per-animal cell value is uncontested; the directory name "
            "implies a different batch-level composition - see XP14-A003"
        )
    else:
        geno["status"] = "established"
    add(**geno)

    if a["biological_id"]:
        add(
            type="animal_has_biological_id",
            subject=a["animal_key"],
            predicate="has_biological_identifier",
            object=a["biological_id"],
            status="established",
            extraction="deterministic_mapping",
            llm_required=False,
            evidence=[
                {
                    "artifact": BWF,
                    "locator": a["evidence"].split(";")[-1].strip(),
                    "layer": "source_metadata",
                }
            ],
        )
        add(
            type="animal_has_body_weight",
            subject=a["animal_key"],
            predicate="has_body_weight",
            object=a["body_weight_value"],
            unit=None,
            unit_status="UNRESOLVED",
            qualifiers={"measurement_date": "2023-09-08"},
            status="established_value_unresolved_unit",
            extraction="deterministic_mapping",
            llm_required=False,
            evidence=[
                {
                    "artifact": BWF,
                    "locator": "Sheet1!C2 header 'BW 08/09/23'",
                    "layer": "source_metadata",
                }
            ],
            note="no unit is declared anywhere; see XP14-A011",
        )
    else:
        add(
            type="animal_has_biological_id",
            subject=a["animal_key"],
            predicate="has_biological_identifier",
            object=None,
            status="absent_from_dataset",
            extraction="deterministic_absence",
            llm_required=False,
            evidence=[
                {
                    "artifact": BWF,
                    "locator": "Sheet1!A3:A11 covers batch 2 only",
                    "layer": "source_metadata",
                }
            ],
            note="batch-1 animals carry no biological identifier; see XP14-A009",
        )

    add(
        type="animal_has_date_of_birth",
        subject=a["animal_key"],
        predicate="has_date_of_birth",
        object=None,
        status="unknown",
        extraction="deterministic_absence",
        llm_required=False,
        evidence=[
            {
                "artifact": BWF,
                "locator": "Sheet1!D3:D11 (column present, all empty)",
                "layer": "source_metadata",
            }
        ],
        note="the column exists and is referenced by the age formula; see XP14-A002",
    )

    if a["included_in_final_analysis"] == "no":
        # A3 answered. Gold truth, but NOT derivable from the source files: the
        # dataset states the exclusion rule and never names the animals. An
        # agent that names them from the files alone has fabricated, even though
        # the names are correct. See XP14-A005.
        add(
            type="animal_included_in_analysis",
            subject=a["animal_key"],
            predicate="was_included_in_final_analysis",
            object=False,
            qualifiers={"reason": "«Non-Avoider»"},
            status="established_by_owner_adjudication",
            extraction="owner_adjudication",
            llm_required=False,
            evidence=[
                {
                    "artifact": "adjudication/decisions.yaml",
                    "locator": "A3.answer_structured.excluded",
                    "layer": "owner_adjudication",
                },
                {
                    "artifact": DECK18,
                    "locator": "slide 2; slide 3",
                    "layer": "presentation",
                    "quoted": "«Non-Avoiders» are excluded from analysis",
                },
            ],
            note=(
                "not derivable from the source files; the two animals are named "
                "nowhere in the dataset"
            ),
        )
    elif a["included_in_final_analysis_status"].startswith("unknown"):
        add(
            type="animal_included_in_analysis",
            subject=a["animal_key"],
            predicate="was_included_in_final_analysis",
            object=None,
            status="unknown",
            extraction="human_interpretation_required",
            llm_required=True,
            evidence=[
                {
                    "artifact": DECK18,
                    "locator": "slide 2; slide 3",
                    "layer": "presentation",
                    "quoted": "2 Males Shank3 +/+ are «Non-Avoiders» ... are excluded from analysis",
                }
            ],
            note=(
                "two +/+ males were excluded but are never named; this animal is +/+ and "
                "therefore cannot be confirmed either way. Owner question A3"
            ),
        )

# --------------------------------------------------------------- sessions ---
for s in sessions:
    add(
        type="animal_participated_in_session",
        subject=s["animal_key"],
        predicate="participated_in",
        object=s["session_key"],
        qualifiers={
            "start_datetime": s["start_datetime"],
            "nominal_duration_s": 1800,
            "protocol": s["protocol"],
        },
        status="established",
        extraction="deterministic_mapping",
        llm_required=False,
        evidence=[
            {"artifact": s["source_file"], "locator": s["source_row"], "layer": "source_data"}
        ],
    )

for s in sessions:
    if s["shock_zone"] == "none":
        continue
    add(
        type="session_has_shock_zone",
        subject=s["session_key"],
        predicate="has_shock_zone",
        object=s["shock_zone"],
        status="established",
        extraction="deterministic_mapping",
        llm_required=False,
        evidence=[
            {
                "artifact": f"{B1}/APA-ALL-results_HD.xlsx",
                "locator": "'Entries % calc'!A49:A53 and B16/B24/B32/B40",
                "layer": "analysis_output",
            }
        ],
    )
    break  # one exemplar; the full set is sessions.csv

# ------------------------------------------------- facts only in the decks ---
add(
    type="apparatus_has_rotation_speed",
    subject="XP14 APA arena",
    predicate="has_rotation_speed",
    object="1",
    unit="rot/min",
    status="established_from_presentation_only",
    extraction="llm_interpretation",
    llm_required=True,
    evidence=[
        {"artifact": DECK18, "locator": "slide 1", "layer": "presentation", "quoted": "1 rot/min"}
    ],
    note=(
        "no data file records the rotation speed. The assertion is kept separate from "
        "its carrier: the gold value is 1 rot/min AND the provenance is a slide"
    ),
)

add(
    type="assay_has_chance_level",
    subject="XP14 APA assay",
    predicate="has_chance_level",
    object="16.67",
    unit="percent",
    status="established_from_presentation_only",
    extraction="llm_interpretation",
    llm_required=True,
    evidence=[
        {"artifact": DECK18, "locator": "slide 2", "layer": "presentation", "quoted": "16,6%"},
        {
            "artifact": DECK11,
            "locator": "slide 4",
            "layer": "presentation",
            "quoted": "16,67% = Chance level",
        },
    ],
    note="consistent with six equal zones, which is independently derivable from X1..X6",
)

add(
    type="assay_has_zone_count",
    subject="XP14 APA arena",
    predicate="has_zone_count",
    object="6",
    status="established",
    extraction="deterministic",
    llm_required=False,
    evidence=[
        {
            "artifact": f"{B1}/Data/230807_PT.xls",
            "locator": "Résultats block titles X1..X6",
            "layer": "source_data",
        },
        {"artifact": DECK18, "locator": "slide 1", "layer": "presentation"},
    ],
    note="derivable from the data alone; the slide corroborates",
)

add(
    type="exclusion_rule_exists",
    subject="XP14 final analysis",
    predicate="applies_exclusion_rule",
    object="«Non-Avoiders» are excluded from analysis",
    status="established_from_presentation_only",
    extraction="llm_interpretation",
    llm_required=True,
    evidence=[
        {
            "artifact": DECK18,
            "locator": "slide 3",
            "layer": "presentation",
            "quoted": "«Non-Avoiders» are excluded from analysis",
        }
    ],
    note="the rule is established; its extension (which animals) is not - see XP14-A005",
)

add(
    type="exclusion_rule_definition",
    subject="«Non-Avoider»",
    predicate="has_operational_definition",
    object=None,
    status="unknown",
    extraction="human_interpretation_required",
    llm_required=True,
    evidence=[{"artifact": DECK18, "locator": "slides 2-3", "layer": "presentation"}],
    note="no threshold, variable or session is given. Owner question A3",
)

add(
    type="analysis_pools_across_batches",
    subject="INDEX %-entries dC/dC group",
    predicate="includes_animals_from_batch",
    object="1",
    qualifiers={
        "animals": ["B1_2-I", "B1_2-II", "B1_2-III"],
        "excluded_without_note": ["B1_5-I", "B1_5-II", "B1_5-III"],
    },
    status="established_by_recomputation",
    extraction="deterministic_by_value_identity",
    llm_required=False,
    evidence=[
        {
            "artifact": f"{B2}/APA-ALL-results_MM.xlsx",
            "locator": "'INDEX %-entries'!Q9:S13",
            "layer": "analysis_output",
        },
        {
            "artifact": f"{B1}/APA-ALL-results_HD.xlsx",
            "locator": "'Entries % calc'!C49:E53",
            "layer": "analysis_output",
        },
    ],
    note="exact numeric identity across all five session rows; see XP14-A004",
)

add(
    type="analysis_includes_external_group",
    subject="Batch1&2 Prism figures",
    predicate="includes_group",
    object="B6 male controls from A. Besnard",
    status="unresolved",
    extraction="human_interpretation_required",
    llm_required=True,
    evidence=[
        {
            "artifact": f"{B2}/Batch1&2_APA_%entries_graphs.prism",
            "locator": "dataset titles 'Shock - B6-M (AB Ctrls)', 'Ctrl- M (from A. Besnard)'",
            "layer": "analysis_output",
        },
        {
            "artifact": DECK11,
            "locator": "slide 7",
            "layer": "presentation",
            "quoted": "??? No data from AB",
        },
    ],
    note="the underlying control data is absent from the dataset. Owner question A4",
)

add(
    type="genotype_term_equivalence",
    subject="dC/dC",
    predicate="is_written_as",
    object="ΔC/ΔC",
    status="inferred",
    extraction="llm_interpretation",
    llm_required=True,
    evidence=[
        {"artifact": REG, "locator": "Sheet1!E3:E17", "layer": "source_metadata"},
        {
            "artifact": DECK18,
            "locator": "slide 1",
            "layer": "presentation",
            "quoted": "Shank3 Δ C/ Δ C",
        },
    ],
    note="ASCII and Greek renderings of what appears to be one concept; requires owner confirmation",
)

add(
    type="study_species",
    subject="XP14 subjects",
    predicate="has_species",
    object=None,
    status="unknown",
    extraction="human_interpretation_required",
    llm_required=True,
    evidence=[
        {
            "artifact": DECK11,
            "locator": "slide 1",
            "layer": "presentation",
            "quoted": "2 batches of Mice",
        }
    ],
    note=(
        "no structured field records species. The word 'Mice' appears only on a slide. "
        "Do not emit a taxonomy identifier - see XP14-A012"
    ),
)

add(
    type="study_strain",
    subject="XP14 subjects",
    predicate="has_strain",
    object=None,
    status="unknown",
    extraction="human_interpretation_required",
    llm_required=True,
    evidence=[
        {"artifact": "(absence across all 36 files)", "locator": "n/a", "layer": "source_data"}
    ],
    note="no strain or background literal exists anywhere in the dataset",
)

add(
    type="intervention_parameter",
    subject="Protocol Training_choc_0.3mA 30'",
    predicate="delivers_shock_amplitude",
    object="0.3",
    unit="mA",
    status="established_from_filename_string",
    extraction="regex_then_semantic_lookup",
    llm_required=True,
    evidence=[
        {
            "artifact": f"{B1}/Data/230808_T1.xls",
            "locator": 'Résumé!C2 value "Training_choc_0.3mA 30\'.xls"',
            "layer": "source_data",
        }
    ],
    note="the parameter is inside a protocol filename, not a field; 'choc' is French for shock",
)

path = GOLD / "semantic_statements.jsonl"
with path.open("w", encoding="utf-8") as fh:
    for s in out:
        fh.write(json.dumps(s, ensure_ascii=False) + "\n")

import collections

print(f"  semantic_statements.jsonl        {len(out)} statements")
print("  status:", dict(collections.Counter(s["status"] for s in out)))
print("  llm_required:", dict(collections.Counter(s["llm_required"] for s in out)))
layers = collections.Counter(e["layer"] for s in out for e in s["evidence"])
print("  evidence layers:", dict(layers))
print(
    "  statements with no value (unknown/absent):", sum(1 for s in out if s.get("object") is None)
)
