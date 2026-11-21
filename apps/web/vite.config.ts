import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Dev: the API runs on 127.0.0.1:8000 (not "localhost": IPv6 forwarding hangs under
// Docker Desktop on Windows). In Compose, nginx serves the build and proxies /api.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  test: { environment: "node" },
});
