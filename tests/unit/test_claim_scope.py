from telemetry_nerd.core.claim_scope import claim_series, read_selector

LB = {"a": {"pod": "x,y", "job": "api"}, "b": {"pod": 'q"z', "job": "web"}}


def names(sel):
    return [str(m) for m in read_selector(sel).matchers]


def test_quoted_values_escapes_and_bare_names():
    assert claim_series(r'up{pod="x,y"}', LB).ids == ["a"]
    assert claim_series(r'up{pod="q\"z"}', LB).ids == ["b"]  # escaped quote
    assert claim_series(r"up{pod='q\"z'}", LB).ids == ["b"]
    assert claim_series(r"up{job=~`a.i`}", LB).ids == ["a"]
    assert names('{"my.metric", job="api"}') == ['__name__="my.metric"', 'job="api"']
    assert names("up") == ['__name__="up"']


def test_bare_names_without_braces():
    assert names("rate(down[5m])") == ['__name__="down"']
    assert names("up offset 5m") == ['__name__="up"']
    assert names("sum by (pod) (rate(x[5m:1m] @ end()))") == ['__name__="x"']
    assert names("histogram_quantile(0.9, sum by (le) (rate(h_bucket[5m])))") == [
        '__name__="h_bucket"'
    ]
    assert read_selector("x * 1e3").matchers is None


def test_missing_label_reads_empty_and_moot_matchers_apply():
    for sel in ['{zone=""}', '{zone=~".*"}', '{zone!="eu"}', '{zone!~"eu.*"}']:
        out = claim_series(sel, LB)
        assert out.ids == ["a", "b"] and out.notes == [], sel
    out = claim_series('{zone="eu"}', LB)
    assert out.ids == ["a", "b"] and out.notes[0][0] == "warn" and "zone" in out.notes[0][1]


def test_name_matchers_need_the_dataset_metric():
    out = claim_series('up{job="api"}', LB)
    assert out.ids == ["a"] and out.notes[0][0] == "warn" and "not checked" in out.notes[0][1]
    assert claim_series('up{job="api"}', LB, metric="up").notes == []
    assert claim_series('{__name__=~"u."}', LB, metric="up").ids == ["a", "b"]
    # checked against the metric, not waved through because "" would not match
    assert claim_series('{__name__!="up"}', LB, metric="up").mismatch


def test_regex_semantics_and_refusals():
    lb = {"a": {"msg": "two\nlines"}}
    assert claim_series('{msg=~"two.lines"}', lb).ids == ["a"]  # . matches newline (?s)
    r = read_selector('{pod=~"[[:alpha:]]+"}')
    assert r.matchers is None and "RE2" in r.why
    r = read_selector(r'{pod=~"\\p{L}"}')
    assert r.matchers is None and "RE2" in r.why
    r = read_selector('{pod=~"(unclosed"}')
    assert r.matchers is None and "invalid" in r.why


def test_expressions_that_combine_series_are_not_read():
    for expr in ["a{pod='z'} + b", 'label_replace(up{pod="z"}, "x", "$1", "pod", "(.*)")',
                 "up{pod='z'} or up{pod='x,y'}", 'up{pod="unterminated}',
                 'count_values("v", up{pod="z"})', "a or b"]:  # fmt: skip
        assert read_selector(expr).matchers is None, expr
    out = claim_series("a + b", LB)
    assert out.ids == ["a", "b"] and out.notes[0][0] == "info"
