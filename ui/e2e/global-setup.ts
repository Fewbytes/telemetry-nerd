import { execSync } from "node:child_process";

export default function globalSetup() {
  // E2E_SKIP_SEED=1: specs that seed their own uniquely named metrics (seasonal) can run without
  // touching the shared demo series
  if (process.env.E2E_SKIP_SEED) return;
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