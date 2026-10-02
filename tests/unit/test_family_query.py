"""Query a name-template family as one metric with a `dimension` label (bead 2as.26)."""

import pytest

from telemetry_nerd.catalog.family_query import (
    FamilyQueryRefused,
    name_regex,
    rewrite,
)
from telemetry_nerd.sources.base import SourceError

from .fakes import FakeSource, make_service
from .test_families import FAMILY, discovery

T = "airflow_ti_finish_*_removed"


def known(t):
    return t == T


def test_name_regex_matches_and_captures_the_slot():
    match, cap = name_regex(T)
    assert match == "airflow_ti_finish_.+_removed" and cap == "airflow_ti_finish_(.+)_removed"
    with pytest.raises(FamilyQueryRefused, match="exactly one slot"):
        name_regex("a_*_b_*_c")


def test_an_instant_selector_gets_the_dimension_label():
    out, used = rewrite(f'sum by (dimension) ({T}{{job="x"}})', known, keeps_names=False)
    assert used == [T]
    assert out == (
        'sum by (dimension) (label_replace({__name__=~"airflow_ti_finish_.+_removed",job="x"}, '
        '"dimension", "$1", "__name__", "airflow_ti_finish_(.+)_removed"))'
    )


def test_a_range_function_keeps_names_only_where_the_engine_can():
    out, _ = rewrite(f"rate({T}[5m])", known, keeps_names=True)
    assert out.startswith(
        'label_replace(rate({__name__=~"airflow_ti_finish_.+_removed"}[5m]) keep_metric_names'
    )
    with pytest.raises(FamilyQueryRefused, match="drop the metric name"):
        rewrite(f"rate({T}[5m])", known, keeps_names=False)


def test_strings_and_unknown_templates_are_left_alone():
    q = 'up{job="a*b"} + other_*_thing + 2*3'
    assert rewrite(q, known, keeps_names=False) == (q, [])
    out, used = rewrite(f'up{{job="{T}"}} + {T}', known, keeps_names=False)
    assert used == [T] and f'job="{T}"' in out  # the literal is intact


# --- through the service -----------------------------------------------------------------------
class Recording(FakeSource):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.exprs: list[str] = []

    async def fetch(self, expr, rng, step_ms):
        self.exprs.append(expr)
        return await super().fetch(expr, rng, step_ms)


@pytest.fixture
async def svc(tmp_path):
    src = Recording(name="default", discovery=discovery())
    s = make_service(tmp_path, src)
    await s.learn("default")
    s.src = src  # type: ignore[attr-defined]
    return s


async def test_a_known_family_is_expanded_before_the_source_sees_it(svc):
    out = await svc.query(f"sum by (dimension) ({FAMILY})", start="now-2h", end="now-1h")
    sent = svc.src.exprs[-1]
    assert 'label_replace({__name__=~"airflow_ti_finish_.+_removed"}' in sent
    assert '"airflow_ti_finish_(.+)_removed"' in sent
    assert out["dataset"]


async def test_an_unknown_template_is_not_ours_to_rewrite(svc):
    await svc.query("nope_*_thing", start="now-2h", end="now-1h")
    assert svc.src.exprs[-1] == "nope_*_thing"


async def test_a_range_function_over_a_family_is_refused_on_prometheus(svc):
    with pytest.raises(SourceError, match="keep_metric_names") as e:
        await svc.query(f"rate({FAMILY}[5m])", start="now-2h", end="now-1h")
    assert e.value.hint
