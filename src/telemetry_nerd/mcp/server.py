"""MCP tools. Results are compact JSON: handles, summaries, caveats, links. Never raw series."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from telemetry_nerd.core.service import ChartRejected, TelemetryService
from telemetry_nerd.model.errors import NotFound
from telemetry_nerd.model.jsonsafe import dumps
from telemetry_nerd.sources.base import SourceError

INSTRUCTIONS = """\
Telemetry Nerd: an evidence-first telemetry workspace shared with the user's browser.
- `query` fetches a PromQL/MetricsQL expression as min/max/avg/count buckets and returns a
  dataset handle plus a compact summary. Write native PromQL/MetricsQL.
- `show` draws a dataset as a panel. Every panel must answer an explicit question; phrase
  it as the question the graph answers. Share the returned URL with the user.
- Report caveats from summaries (gaps, settling, fake_resolution) when you describe data.
- Scope every claim: source, selector, time range, step. Do not generalize beyond it.
"""


def _dump(obj: dict) -> str:
    return dumps(obj, separators=(",", ":"))


def build_mcp(service: TelemetryService, ui_url: str) -> MCPServer:
    mcp = MCPServer("telemetry-nerd", instructions=INSTRUCTIONS)

    @mcp.tool()
    async def query(
        expr: str,
        start: str = "now-1h",
        end: str = "now",
        step: str = "auto",
        source: str = "default",
    ) -> str:
        """Fetch a PromQL/MetricsQL expression as a dataset of min/max/avg/count buckets.

        start/end: `now`, `now-<dur>` (e.g. now-6h), epoch ms, or ISO-8601 with timezone.
        step: `auto` (~600 buckets) or a duration like 30s, 1m, 5m.
        Returns {dataset, summary}. The summary is compact; raw series stay on the server.
        """
        try:
            # pi-lens-ignore: python-sql-injection
            return _dump(await service.query(expr, start, end, step, source))
        except SourceError as e:
            raise ToolError(f"{e} (hint: {e.hint})" if e.hint else str(e)) from e
        except ValueError as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    def show(dataset: str, question: str) -> str:
        """Draw a dataset as a panel (mean line + min/max envelope) in the shared workspace.

        question is REQUIRED: the explicit question this graph answers, e.g.
        "Did checkout latency rise after the 14:00 deploy?".
        Returns {panel, url, warnings}.
        """
        try:
            res = service.show(dataset, question)
        except ChartRejected as e:
            raise ToolError(f"chart rejected: {e}") from e
        except (NotFound, ValueError) as e:
            raise ToolError(str(e)) from e
        return _dump(
            {
                "panel": res.panel.id,
                "url": f"{ui_url}/#/panel/{res.panel.id}",
                "warnings": [i.message for i in res.issues],
            }
        )

    return mcp
