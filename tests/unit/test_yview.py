import pyarrow as pa
import pytest
from pydantic import ValidationError

from telemetry_nerd.charts.spec import ChartSpec
from telemetry_nerd.charts.yview import YView, check_view, value_stats
from telemetry_nerd.model.series import BUCKET_SCHEMA


def buckets(avg, count=None, spread=0.0):
    n = len(avg)
    lo = [None if v is None else v - spread for v in avg]
    hi = [None if v is None else v + spread for v in avg]
    return pa.table(
        {
            "ts_ms": list(range(n)),
            "series_id": ["s"] * n,
            "avg": avg,
            "min": lo,
            "max": hi,
            "count": count or [4] * n,
        },
        schema=BUCKET_SCHEMA,
    )


P8 = buckets([0.4, 1.3, 34.0, 0.9, None], count=[300, 250, 13, 400, None])


def test_quantile_stats_split_meaningful_from_faded():
    st = value_stats(P8, "quantile", 200)
    assert (st.lo, st.hi) == (0.4, 34.0)
    assert (st.meaningful_lo, st.meaningful_hi) == (0.4, 1.3)
    assert st.quantile and st.low_n == 1  # the null-count bucket has no value; it is not drawn


def test_envelope_counts_for_plain_series():
    st = value_stats(buckets([1.0, 2.0], spread=0.5), "bucket_agg", None)
    assert (st.lo, st.hi) == (0.5, 2.5) and not st.quantile and st.meaningful_lo is None


def test_label_reason_and_band_shape():
    with pytest.raises(ValidationError):
        YView(mode="data", label="  ")
    with pytest.raises(ValidationError):
        YView(mode="band", label="b", lo=2, hi=1)
    with pytest.raises(ValidationError):
        YView(mode="data", label="d", lo=1, hi=2)  # lo/hi only for band
    with pytest.raises(ValidationError, match="reason"):
        YView(mode="meaningful", label="m", author="claude")
    with pytest.raises(ValidationError, match="one line"):
        YView(mode="meaningful", label="m", reason="a\nb", author="claude")


def test_log_refused_on_non_positive_values():
    st = value_stats(buckets([0.0, 5.0]), "bucket_agg", None)
    with pytest.raises(ValueError, match="log"):
        check_view(YView(mode="log", label="log scale"), st)
    assert check_view(YView(mode="log", label="log"), value_stats(P8, "quantile", 200)) == []


def test_meaningful_only_for_quantiles_with_meaningful_buckets():
    with pytest.raises(ValueError, match="percentile"):
        check_view(
            YView(mode="meaningful", label="m"), value_stats(buckets([1.0]), "bucket_agg", None)
        )
    none_ok = value_stats(buckets([1.0], count=[5]), "quantile", 200)
    with pytest.raises(ValueError, match="n ≥ 200|no bucket"):
        check_view(YView(mode="meaningful", label="m"), none_ok)


def test_band_must_overlap_data_and_warns_when_it_extends_past_it():
    st = value_stats(P8, "quantile", 200)
    with pytest.raises(ValueError, match="no data"):
        check_view(YView(mode="band", label="b", lo=50, hi=60), st)
    assert check_view(YView(mode="band", label="b", lo=0.3, hi=1.5), st) == [
        "band extends below the data (min 0.4)"
    ]


def test_spec_round_trips_views():
    spec = ChartSpec.model_validate({"layers": [{"mark": "line+envelope", "data": "d1"}]})
    assert spec.y.views == [] and spec.y.selected is None
