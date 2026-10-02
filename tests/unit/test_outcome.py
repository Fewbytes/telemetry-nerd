"""Latency split by outcome (bead 2as.19)."""

import pytest

from telemetry_nerd.analysis.histogram import from_matrix, histogram_expr
from telemetry_nerd.analysis.outcome import (
    add_matcher,
    candidate_labels,
    classify,
    classify_value,
)
from telemetry_nerd.model.discovery import Discovery

from .fakes import FakeSource, make_service


def test_http_codes_split_into_success_and_failure_and_client_errors_are_left_out():
    o = classify("code", ["200", "204", "301", "404", "429", "500", "503"])
    assert o.success == ("200", "204", "301")
    assert o.failure == ("500", "503")
    assert o.excluded == ("404", "429")  # the client's doing: neither a success nor our failure


@pytest.mark.parametrize(
    ("label", "value", "want"),
    [
        ("outcome", "success", "success"),
        ("outcome", "timeout", "failure"),
        ("result", "ok", "success"),
        ("status", "OK", "success"),
        ("status", "DEADLINE_EXCEEDED", "failure"),
        ("status", "0", "success"),  # gRPC OK
        ("success", "true", "success"),
        ("success", "false", "failure"),
        ("success", "maybe", None),
        ("error", "", "success"),
        ("error", "TimeoutError", "failure"),
        ("outcome", "weird", None),
    ],
)
def test_words_and_booleans(label, value, want):
    assert classify_value(label, value) == want


def test_candidate_labels_keep_trial_order_and_ignore_the_rest():
    assert candidate_labels(["job", "code", "status_code", "le"]) == ["status_code", "code"]
    assert candidate_labels(["job", "instance"]) == []


def test_add_matcher_extends_a_plain_selector_and_escapes():
    assert add_matcher("x_bucket", "code", ("500", "503")) == 'x_bucket{code=~"500|503"}'
    assert add_matcher('x_bucket{job="a"}', "code", ("5.0",)) == 'x_bucket{job="a",code=~"5\\.0"}'
    with pytest.raises(ValueError, match="plain selector"):
        add_matcher("sum(x_bucket)", "code", ("500",))


# --- through the service -------------------------------------------------------------------------
class OutcomeSource(FakeSource):
    """A latency histogram whose requests end 200, 404 or 503."""

    codes = ("200", "404", "503")

    async def discover(self):
        return Discovery((), ("job", "code", "le"), {}, None, 1.0, (), False)

    async def fetch_histogram(self, selector, by, rng, step_ms):
        self.hist_selectors.append(selector)
        ts = range(rng.start_ms, rng.end_ms + 1, step_ms)
        result = [
            {
                "metric": {**({by[0]: code} if by else {}), "le": le},
                "values": [[t / 1000, str(c)] for t in ts],
            }
            for code in (self.codes if by else ("200",))
            for le, c in self.cumulative.items()
        ]
        return from_matrix(self.name, result, expr=histogram_expr(selector, by, step_ms))


@pytest.fixture
def src():
    return OutcomeSource(name="default")


@pytest.fixture
def svc(tmp_path, src):
    return make_service(tmp_path, src)


async def test_a_distribution_is_split_into_two_panels_with_the_values_used(svc, src):
    ds = (await svc.query_distribution('http_dur_bucket{job="a"}', start="now-2h"))["dataset"]
    out = await svc.split_outcome(ds, "user")
    assert out["label"] == "code" and out["excluded"] == ["404"]
    assert out["success"]["values"] == ["200"] and out["failure"]["values"] == ["503"]
    assert 'http_dur_bucket{job="a",code=~"200"}' in src.hist_selectors
    assert 'http_dur_bucket{job="a",code=~"503"}' in src.hist_selectors
    ok, bad = (svc.workspace.get_panel(out[k]["panel"]) for k in ("success", "failure"))
    assert "successful" in ok.question and "failed" in bad.question
    assert ok.dataset_ids != bad.dataset_ids and svc.datasets.exists(ok.dataset_ids[0])


async def test_a_quantile_series_is_split_by_rewriting_its_selector(svc, src):
    expr = 'histogram_quantile(0.99, sum by (le) (rate(http_dur_bucket{job="a"}[5m])))'
    ds = (await svc.query(expr, start="now-2h", end="now-1h", step="1m"))["dataset"]
    out = await svc.split_outcome(ds, "claude")
    assert out["success"] and out["failure"]
    assert any('http_dur_bucket{job="a",code=~"503"}' in e for e in src.value_exprs)


async def test_no_failures_says_so_instead_of_inventing_a_panel(svc, src):
    src.codes = ("200", "204")
    ds = (await svc.query_distribution("http_dur_bucket", start="now-2h"))["dataset"]
    out = await svc.split_outcome(ds, "user")
    assert out["success"] and out["failure"] is None and "no failed requests" in out["note"]


async def test_refusals_are_specific(svc, src):
    plain = (await svc.query("up", start="now-2h", end="now-1h"))["dataset"]
    with pytest.raises(ValueError, match="histogram-backed"):
        await svc.split_outcome(plain, "user")
    ds = (await svc.query_distribution("http_dur_bucket", start="now-2h"))["dataset"]

    async def no_labels():
        return Discovery((), ("job",), {}, None, 1.0, (), False)

    src.discover = no_labels  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="no outcome-like label"):
        await svc.split_outcome(ds, "user")
    src.codes = ("404", "weird")

    async def with_codes():
        return Discovery((), ("code",), {}, None, 1.0, (), False)

    src.discover = with_codes  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="classifiable"):
        await svc.split_outcome(ds, "user")
