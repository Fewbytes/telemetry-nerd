import { execSync } from "node:child_process";

export default function globalSetup() {
  try {
    execSync("uv run python scripts/seed_synthetic.py --url http://127.0.0.1:8428 --hours 6", {
      cwd: "..",
      stdio: "inherit",
    });
  } catch (e) {
    throw new Error(
      `seeding synthetic series failed — is VictoriaMetrics up? (just dev-up) — ${String(e)}`,
    );
  }
}