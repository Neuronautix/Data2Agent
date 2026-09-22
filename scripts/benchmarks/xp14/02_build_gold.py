"""Generate the XP14 gold-standard tables from the frozen snapshot.

Everything here is derived from the source bytes and re-verified on each run, so
the gold standard is reproducible rather than hand-transcribed. Facts that the
files cannot settle are emitted with an explicit pending/unknown status and are
never filled in.
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

import openpyxl

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
PKG = REPO / "benchmarks" / "xp14_apa"
SRC = PKG / "source"
GOLD = PKG / "gold"
GOLD.mkdir(parents=True, exist_ok=True)

B1 = "230807-11_APA-1_3M-3F-dCdC (By M Marias)"
B2 = "230904-08_APA-2_3M++-6M-dCdC"
IDS = SRC / "Batch APA-1&2 Results" / "IDs Batch Sex Group.xlsx"
BW = SRC / B2 / "APA_Batch2_BW_IDs.xlsx"


def load(path: Path, sheet: str, data_only: bool = True):
    raw = path.read_bytes()  # bypass extension sniffing; content is OOXML
    return openpyxl.load_workbook(io.BytesIO(raw), data_only=data_only)[sheet]


def write_csv(name: str, rows: list[dict], fieldnames: list[str]) -> None:
    with (GOLD / name).open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"  {name:<32} {len(rows)} rows")


# ---------------------------------------------------------------- animals ---
ids = load(IDS, "Sheet1")
bwv = load(BW, "Sheet1")

bw_map = {}
for r in range(3, 12):
    local = str(bwv.cell(r, 2).value).strip()
    bw_map[local] = {
        "idreal": bwv.cell(r, 1).value,
        "bw": bwv.cell(r, 3).value,
        "dob": bwv.cell(r, 4).value,
        "geno": bwv.cell(r, 6).value,
        "row": r,
    }

animals = []
for r in range(3, 18):
    batch = str(ids.cell(r, 2).value)
    local = str(ids.cell(r, 4).value).strip()
    geno = ids.cell(r, 5).value
    sex = ids.cell(r, 6).value
    animal_key = f"B{batch}_{local}"

    # the 0 <-> O question (owner question A2) is recorded, never applied silently
    alt = local.replace("-0", "-O")
    # APA_Batch2_BW_IDs.xlsx covers batch 2 ONLY. Keying on local_id alone would
    # match batch-1 '2-I'/'2-II' to batch-2 animals, because those literals
    # collide across batches -- the defect this dataset is meant to expose.
    bw_hit = bw_map.get(local) if batch == "2" else None
    join_note = ""
    if batch == "2" and bw_hit is None and alt in bw_map:
        bw_hit = bw_map[alt]
        join_note = (
            f"joined to APA_Batch2_BW_IDs.xlsx row {bw_hit['row']} only after "
            f"substituting digit-0 with letter-O ('{local}' -> '{alt}'); PENDING-A2"
        )

    geno_bw = bw_hit["geno"] if bw_hit else ""
    genotype_status = "established"
    if batch == "2":
        # two workbooks agree per animal; the folder name implies a different
        # overall composition. The conflict is at the count level, not the cell.
        genotype_status = "established_cell_value__composition_conflict_A1"

    analysed_status = "established"
    if batch == "2" and geno == "+/+":
        analysed_status = "unknown_pending_A3"

    animals.append({
        "animal_key": animal_key,
        "batch": batch,
        "local_id": local,
        "local_id_alt_spelling": alt if alt != local else "",
        "biological_id": bw_hit["idreal"] if bw_hit else "",
        "biological_id_status": "established" if bw_hit else "absent_from_dataset",
        "sex": sex,
        "sex_status": "established",
        "genotype": geno,
        "genotype_status": genotype_status,
        "genotype_corroborating_source": geno_bw,
        "body_weight_value": bw_hit["bw"] if bw_hit else "",
        "body_weight_unit": "UNRESOLVED",
        "body_weight_date": "2023-09-08" if bw_hit else "",
        "date_of_birth": "",
        "date_of_birth_status": "absent_column_present_but_empty",
        "age_at_testing": "",
        "age_status": "not_computable__DOB_absent",
        "included_in_final_analysis": "yes" if analysed_status == "established" else "unknown",
        "included_in_final_analysis_status": analysed_status,
        "species": "UNRESOLVED",
        "strain": "UNRESOLVED",
        "evidence": (
            f"IDs Batch Sex Group.xlsx!Sheet1!r{r}"
            + (f"; APA_Batch2_BW_IDs.xlsx!Sheet1!r{bw_hit['row']}" if bw_hit else "")
        ),
        "notes": join_note,
    })

write_csv("animals.csv", animals, list(animals[0].keys()))

# ------------------------------------------------------------- crosswalk ---
SEA = {
    ("1", "PT"): "22.sea", ("1", "T1"): "T1.sea", ("1", "T2"): "230809_T2.sea",
    ("1", "R1"): "230810_R1.sea", ("1", "R2"): "230811_R2.sea",
    ("2", "PT"): "PT.sea", ("2", "T1"): "T1.sea", ("2", "T2"): "T2.sea",
    ("2", "R1"): "R1.sea", ("2", "R2"): "R2.sea",
}
FILES = {
    ("1", "PT"): f"{B1}/Data/230807_PT.xls", ("1", "T1"): f"{B1}/Data/230808_T1.xls",
    ("1", "T2"): f"{B1}/Data/230809_T2.xls", ("1", "R1"): f"{B1}/Data/230810_R1.xls",
    ("1", "R2"): f"{B1}/Data/230811_R2.xls",
    ("2", "PT"): f"{B2}/Data/230904_PT.xls", ("2", "T1"): f"{B2}/Data/230905_T1.xls",
    ("2", "T2"): f"{B2}/Data/230906_T2.xls", ("2", "R1"): f"{B2}/Data/230907_R1.xls",
    ("2", "R2"): f"{B2}/Data/230908_R2.xls",
}
SESSIONS = ["PT", "T1", "T2", "R1", "R2"]

# read every Resume sheet once: (batch, session) -> [(seance, animal_literal, dt, exercice, duree)]
resume: dict[tuple[str, str], list] = {}
for (batch, sess), relpath in FILES.items():
    ws = load(SRC / relpath, "Résumé")
    rows = []
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, 1).value is None:
            continue
        rows.append((
            ws.cell(r, 1).value,                      # FICHIER
            ws.cell(r, 2).value,                      # Numero seance
            ws.cell(r, 3).value,                      # Exercice
            ws.cell(r, 4).value,                      # Duree exp
            ws.cell(r, 5).value,                      # Date
            str(ws.cell(r, 6).value).strip(),         # Animal (leading space in source)
            r,
        ))
    resume[(batch, sess)] = rows

cross = []
for a in animals:
    batch, local = a["batch"], a["local_id"]
    seance = ""
    for fichier, num, _ex, _du, _dt, animal, _r in resume[(batch, "PT")]:
        if animal == local:
            seance = num
    stem = SEA[(batch, "PT")].replace(".sea", "")
    cross.append({
        "animal_key": a["animal_key"],
        "biological_id": a["biological_id"],
        "local_id": local,
        "batch": batch,
        "anymaze_composite_id": a["animal_key"],
        "seance_number": seance,
        "result_column_id_PT": f"{stem}_sea{seance}" if seance else "",
        "sea_files": "; ".join(SEA[(batch, s)] for s in SESSIONS),
        "local_id_unique_globally": "no" if any(
            b["local_id"] == local and b["batch"] != batch for b in animals) else "yes",
        "collides_with": "; ".join(
            b["animal_key"] for b in animals
            if b["local_id"] == local and b["batch"] != batch),
        "join_caveat": a["notes"],
        "evidence": a["evidence"],
    })
write_csv("identifier_crosswalk.csv", cross, list(cross[0].keys()))

# -------------------------------------------------------------- sessions ---
SHOCK = {"PT": "none", "T1": "X1", "T2": "X1", "R1": "X4", "R2": "X4"}
sessions_rows = []
for (batch, sess), rows in sorted(resume.items(), key=lambda kv: (kv[0][0], SESSIONS.index(kv[0][1]))):
    for fichier, num, exercice, duree, dt, animal, r in rows:
        amp = re.search(r"([\d.]+)\s*mA", str(exercice))
        sessions_rows.append({
            "session_key": f"B{batch}_{sess}_sea{num}",
            "animal_key": f"B{batch}_{animal}",
            "batch": batch,
            "session_label": sess,
            "session_order": SESSIONS.index(sess) + 1,
            "sea_file": fichier,
            "seance_number": num,
            "result_column_id": f"{str(fichier).replace('.sea', '')}_sea{num}",
            "protocol": exercice,
            "start_datetime": str(dt).strip(),
            "nominal_duration": str(duree),
            "nominal_duration_seconds": 1800,
            "shock_zone": SHOCK[sess],
            "shock_amplitude_mA": amp.group(1) if amp else "",
            "shock_amplitude_source": "parsed from the protocol filename string" if amp else "n/a (no shock)",
            "source_file": FILES[(batch, sess)],
            "source_row": f"Résumé!r{r}",
            "status": "established",
        })
write_csv("sessions.csv", sessions_rows, list(sessions_rows[0].keys()))

print()
print("cross-checks:")
print("  animals:", len(animals), "| batch1:", sum(1 for a in animals if a["batch"] == "1"),
      "batch2:", sum(1 for a in animals if a["batch"] == "2"))
import collections
print("  genotype:", dict(collections.Counter(a["genotype"] for a in animals)))
print("  sex:", dict(collections.Counter(a["sex"] for a in animals)))
print("  sessions:", len(sessions_rows), "(expected 75)")
print("  shock zones:", dict(collections.Counter(s["shock_zone"] for s in sessions_rows)))
print("  colliding local ids:", sorted({c["local_id"] for c in cross if c["local_id_unique_globally"] == "no"}))
print("  join caveats:", [c["animal_key"] for c in cross if c["join_caveat"]])
print("  animals with no biological id:", [a["animal_key"] for a in animals if not a["biological_id"]])
print("  analysis inclusion unknown:", [a["animal_key"] for a in animals
                                        if a["included_in_final_analysis_status"].startswith("unknown")])
