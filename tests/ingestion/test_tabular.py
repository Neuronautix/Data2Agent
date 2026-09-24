"""Acceptance: table schema and missingness are extracted correctly, and nothing more."""

from __future__ import annotations

from pathlib import Path

from data2agent.ingest import conventions
from data2agent.ingest.tabular import profile_table


def _columns(profile):
    return {column.name: column for column in profile.columns}


def test_schema_is_read_from_the_header(example_dataset: Path):
    profile = profile_table(example_dataset / "animals.csv", "animals.csv")
    assert [column.name for column in profile.columns] == [
        "animal_id",
        "strain",
        "genotype",
        "sex",
        "birth_date",
        "weight_g",
    ]
    assert profile.rows == 48
    assert profile.delimiter == ","
    assert profile.ragged_rows == 0


def test_missingness_separates_empty_cells_from_resolved_tokens(example_dataset: Path):
    columns = _columns(profile_table(example_dataset / "animals.csv", "animals.csv"))

    assert columns["sex"].missing == 12
    assert columns["sex"].missing_empty == 12
    assert columns["sex"].missing_sentinel == 0
    assert columns["sex"].values == 36

    # 'strain' holds three literal 'NA' tokens. The default convention resolves
    # them to missing -- and records that it did, and which tokens they were.
    assert columns["strain"].missing == 3
    assert columns["strain"].missing_empty == 0
    assert columns["strain"].missing_sentinel == 3
    assert columns["strain"].sentinel_tokens_seen == {"NA": 3}


def test_the_strict_convention_resolves_nothing(example_dataset: Path):
    """Change the convention and the numbers change -- visibly, with a reason."""
    columns = _columns(
        profile_table(example_dataset / "animals.csv", "animals.csv", conventions.STRICT_CONVENTION)
    )
    assert columns["strain"].missing == 0
    assert columns["strain"].missing_sentinel == 0
    # The token is still reported; it is simply not resolved.
    assert columns["strain"].ambiguous_tokens_seen == {}
    assert "NA" in (columns["strain"].distinct_values or [])


def test_a_custom_convention_resolves_the_tokens_it_names(tmp_path: Path):
    path = tmp_path / "c.csv"
    path.write_text("v\n1\nmissing\n3\n", encoding="utf-8")

    default = _columns(profile_table(path, "c.csv"))["v"]
    assert default.missing == 0, "'missing' is not a built-in sentinel"

    declared = _columns(profile_table(path, "c.csv", conventions.custom(["missing"])))["v"]
    assert declared.missing == 1
    assert declared.sentinel_tokens_seen == {"missing": 1}
    # With the token resolved away, the column's real shape becomes visible.
    assert declared.dtype == "integer"


def test_ambiguous_tokens_are_reported_but_never_resolved(tmp_path: Path):
    """'unknown' may be a considered statement, not an absence. We do not decide."""
    path = tmp_path / "a.csv"
    path.write_text("v\nM\nunknown\n?\nF\n", encoding="utf-8")
    column = _columns(profile_table(path, "a.csv"))["v"]

    assert column.missing == 0
    assert column.missing_sentinel == 0
    assert column.ambiguous_tokens_seen == {"unknown": 1, "?": 1}
    assert column.values == 4


def test_a_resolved_sentinel_contributes_no_type(tmp_path: Path):
    """A cell resolved to missing is absent, so it cannot make a column 'string'."""
    path = tmp_path / "w.csv"
    path.write_text("weight_g\n18\nNA\n22\n", encoding="utf-8")
    column = _columns(profile_table(path, "w.csv"))["weight_g"]
    assert column.dtype == "integer"
    assert column.missing == 1
    assert column.distinct_values == ["18", "22"]


def test_dtype_describes_token_shape_not_meaning(example_dataset: Path):
    columns = _columns(profile_table(example_dataset / "animals.csv", "animals.csv"))
    assert columns["weight_g"].dtype == "integer"
    # A date is a string until something states a date format; we do not parse
    # one out of a plausible-looking value.
    assert columns["birth_date"].dtype == "string"
    assert columns["sex"].dtype == "string"


def test_mixed_tokens_collapse_to_string(tmp_path: Path):
    path = tmp_path / "mixed.csv"
    path.write_text("a,b\n1,1\n2,x\n", encoding="utf-8")
    columns = _columns(profile_table(path, "mixed.csv"))
    assert columns["a"].dtype == "integer"
    assert columns["b"].dtype == "string"


def test_integers_and_decimals_promote_to_number(tmp_path: Path):
    path = tmp_path / "n.csv"
    path.write_text("v\n1\n2.5\n-3\n1e4\n", encoding="utf-8")
    assert _columns(profile_table(path, "n.csv"))["v"].dtype == "number"


def test_an_all_empty_column_is_empty_not_guessed(tmp_path: Path):
    path = tmp_path / "e.csv"
    path.write_text("a,b\n1,\n2,\n", encoding="utf-8")
    columns = _columns(profile_table(path, "e.csv"))
    assert columns["b"].dtype == "empty"
    assert columns["b"].missing == 2


def test_ragged_rows_are_reported_not_padded(tmp_path: Path):
    path = tmp_path / "r.csv"
    path.write_text("a,b,c\n1,2,3\n4,5\n", encoding="utf-8")
    profile = profile_table(path, "r.csv")
    assert profile.ragged_rows == 1
    assert any("do not have 3 fields" in warning for warning in profile.warnings)


def test_distinct_counts_declare_whether_they_are_exact(example_dataset: Path):
    columns = _columns(profile_table(example_dataset / "animals.csv", "animals.csv"))
    assert columns["genotype"].distinct_exact is True
    assert sorted(columns["genotype"].distinct_values) == ["KO", "WT"]
    # Above the enumeration cap the count is an upper bound and must say so.
    assert columns["animal_id"].distinct_exact is False
    assert columns["animal_id"].distinct_values is None


def test_tsv_delimiter_comes_from_the_extension(tmp_path: Path):
    path = tmp_path / "t.tsv"
    path.write_text("a\tb\n1\t2\n", encoding="utf-8")
    assert profile_table(path, "t.tsv").delimiter == "\t"


def test_semicolon_delimited_csv_overrides_the_extension_hint(tmp_path: Path):
    """A .csv suffix must not collapse a real semicolon table to one column."""
    path = tmp_path / "roche.csv"
    path.write_text(
        "Animal ID;DLC file;Group;Dosage\n"
        "16459-67049;a.csv;Control;0\n"
        "16459-67050;b.csv;Yohimbine;1\n",
        encoding="utf-8",
    )

    profile = profile_table(path, "roche.csv")

    assert profile is not None
    assert profile.delimiter == ";"
    assert [column.name for column in profile.columns] == [
        "Animal ID",
        "DLC file",
        "Group",
        "Dosage",
    ]
    assert any("extension implies delimiter" in warning for warning in profile.warnings)


def test_an_undeterminable_delimiter_yields_no_table(tmp_path: Path):
    """Better to report nothing than to force prose into a table shape."""
    path = tmp_path / "prose.dat"
    path.write_text("this is a sentence\nand another one entirely\n", encoding="utf-8")
    assert profile_table(path, "prose.dat") is None
