"""One _field_caps GET and one _search POST against CERN's esnet index through the public Grafana
datasource proxy: does the proxy forward the paths the Elasticsearch adapter needs?

Run: uv run scripts/cern_es_probe.py
"""

from __future__ import annotations

import json
import time

import httpx

from telemetry_nerd.sources.promql import USER_AGENT

PROXY = "https://monit-grafana-open.cern.ch/api/datasources/proxy/uid/000007855"
PATTERN = "esnet_*"
TIME_FIELD = "timestamp"


def main() -> int:
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(timeout=60, headers=headers) as c:
        caps = c.get(f"{PROXY}/{PATTERN}/_field_caps", params={"fields": TIME_FIELD})
        print("_field_caps", caps.status_code, caps.text[:300])
        time.sleep(1)  # CERN politeness: one request at a time, >= 1 s apart
        body = {"size": 0, "track_total_hits": False, "query": {"match_all": {}}}
        search = c.post(f"{PROXY}/{PATTERN}/_search", content=json.dumps(body),
                        headers={"Content-Type": "application/json"})  # fmt: skip
        print("_search", search.status_code, search.text[:300])
    ok = caps.status_code == 200 and search.status_code == 200
    print("CERN tier:", "add the live test" if ok else "skip (proxy refuses a path)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
