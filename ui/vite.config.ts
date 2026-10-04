/// <reference types="vitest/config" />
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [svelte()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:7070",
      "/ws": { target: "ws://127.0.0.1:7070", ws: true },
    },
  },
  // shuffled order surfaces order-dependent tests; the seed is printed, replay with --sequence.seed=<n>
  test: { include: ["src/**/*.test.ts"], sequence: { shuffle: true } },
});
