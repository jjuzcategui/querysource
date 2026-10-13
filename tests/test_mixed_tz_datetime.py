import pandas as pd

from querysource.types.dt.transforms import (
    case,
    convert_to_datetime,
    fill_column,
    fill_nulls,
    to_timestamp,
)


def _df():
    return pd.DataFrame({"d": [
        "2025-09-15T11:23:30.231-07:00",
        "2025-12-01T08:00:00.000-08:00",
        None,
    ]})


def test_convert_to_datetime_mixed_offsets():
    df = convert_to_datetime(_df(), "d")
    assert df["d"][0] == pd.Timestamp("2025-09-15T11:23:30.231-07:00")
    assert df["d"][0].utcoffset() == pd.Timedelta(hours=-7)
    assert df["d"][1].utcoffset() == pd.Timedelta(hours=-8)
    assert pd.isna(df["d"][2])


def test_to_timestamp_mixed_offsets():
    df = to_timestamp(_df(), "d")
    assert df["d"][1] == pd.Timestamp("2025-12-01T08:00:00-08:00")


def test_fill_nulls_non_string_value_on_string_column():
    df = pd.DataFrame({"w": pd.array(["Juan", None, ""], dtype="string")})
    df = fill_column(df, "nulls", False)
    df = fill_nulls(df, "w", "nulls")
    assert df["w"].tolist() == ["Juan", False, False]
    df = case(df, "erp", column="w", condition=False, match=False, notmatch=True)
    assert df["erp"].tolist() == [True, False, False]
