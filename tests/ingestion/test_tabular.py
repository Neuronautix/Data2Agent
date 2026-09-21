"""Acceptance: table schema and missingness are extracted correctly, and nothing more."""

from __future__ import annotations

from pathlib import Path

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


def test_missingness_counts_empty_cells_only(example_dataset: Path):
    columns = _columns(profile_table(example_dataset / "animals.csv", "animals.csv"))
    assert columns["sex"].missing == 12
    assert columns["sex"].non_empty == 36
    # 'strain' holds three literal 'NA' tokens. They are NOT missing values --
    # nothing in the dataset says what 'NA' means here.
    assert columns["strain"].missing == 0
    assert columns["strain"].null_like == 3


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


def test_an_undeterminable_delimiter_yields_no_table(tmp_path: Path):
    """Better to report nothing than to force prose into a table shape."""
    path = tmp_path / "prose.dat"
    path.write_text("this is a sentence\nand another one entirely\n", encoding="utf-8")
    assert profile_table(path, "prose.dat") is None
