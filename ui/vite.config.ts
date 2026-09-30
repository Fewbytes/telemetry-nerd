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
  test: { include: ["src/**/*.test.ts"] },
});
