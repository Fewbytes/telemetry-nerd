from telemetry_nerd.core.claim_scope import claim_series, read_selector

LB = {"a": {"pod": "x,y", "job": "api"}, "b": {"pod": "z", "job": "web"}}


def test_quoted_values_escapes_and_bare_names():
    assert claim_series(r'up{pod="x,y"}', LB).ids == ["a"]
    assert claim_series("up{pod='z'}", LB).ids == ["b"]
    assert claim_series(r"up{job=~`a.i`}", LB).ids == ["a"]
    assert [str(m) for m in read_selector('{"my.metric", job="api"}').matchers] == [
        '__name__="my.metric"',
        'job="api"',
    ]
    assert [str(m) for m in read_selector("up").matchers] == ['__name__="up"']


def test_missing_label_reads_empty_and_moot_matchers_apply():
    assert claim_series('up{zone=""}', LB).ids == ["a", "b"]  # empty satisfies it: moot
    out = claim_series('{zone="eu"}', LB)
    assert out.ids == ["a", "b"] and "zone" in out.notes[0]


def test_name_matchers_need_the_dataset_metric():
    out = claim_series('up{job="api"}', LB)
    assert out.ids == ["a"] and "not checked" in out.notes[0]
    assert claim_series('up{job="api"}', LB, metric="up").notes == []
    assert claim_series('{__name__=~"u."}', LB, metric="up").ids == ["a", "b"]


def test_expressions_that_combine_series_are_not_read():
    for expr in ["a{pod='z'} + b", 'label_replace(up{pod="z"}, "x", "$1", "pod", "(.*)")',
                 "up{pod='z'} or up{pod='x,y'}", 'up{pod="unterminated}']:  # fmt: skip
        assert read_selector(expr).matchers is None, expr
    assert claim_series("a + b", LB).ids == ["a", "b"]
