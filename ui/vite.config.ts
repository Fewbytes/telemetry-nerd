/// <reference types="vitest/config" />
import { execSync } from "node:child_process";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

// The short commit this build was made from, so the running UI can say what it is (not stale-
// build guessing). "unknown" outside a git checkout (e.g. an extracted release tarball).
const gitSha = (() => {
  try {
    return execSync("git rev-parse --short HEAD", { cwd: import.meta.dirname }).toString().trim();
  } catch {
    return "unknown";
  }
})();

export default defineConfig({
  plugins: [svelte()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:7070",
      "/ws": { target: "ws://127.0.0.1:7070", ws: true },
    },
  },
  define: { __GIT_SHA__: JSON.stringify(gitSha) },
  // shuffled order surfaces order-dependent tests; the seed is printed, replay with --sequence.seed=<n>
  test: { include: ["src/**/*.test.ts"], sequence: { shuffle: true } },
});
