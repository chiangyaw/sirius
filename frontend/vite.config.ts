import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

// Dev: proxy API + WebSocket to the FastAPI backend on :8010.
// (:8000 is occupied by the separate Alfred app on this machine.)
// Build: emit into backend/sirius/web_dist so `sirius serve` can serve it.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8010",
      "/health": "http://127.0.0.1:8010",
      "/ws": { target: "ws://127.0.0.1:8010", ws: true },
    },
  },
  build: {
    outDir: resolve(__dirname, "../backend/sirius/web_dist"),
    emptyOutDir: true,
  },
});
