"""Push synthetic demo series into VictoriaMetrics."""

import argparse

from telemetry_nerd.devtools.synthetic import demo_text, push
from telemetry_nerd.model.time import now_ms


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8428")
    parser.add_argument("--hours", type=int, default=6)
    args = parser.parse_args()
    end = now_ms() // 15_000 * 15_000
    start = end - args.hours * 3_600_000
    push(args.url, demo_text(start, end))
    print(f"seeded {args.hours}h of demo series into {args.url}")


if __name__ == "__main__":
    main()
