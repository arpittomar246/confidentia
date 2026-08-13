import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The dev server proxies /api to the backend rather than calling it cross-origin.
// The API does allow http://localhost:5173 in `api.cors_origins`, so this is not
// load-bearing -- but a same-origin dev setup means a CORS misconfiguration in a
// deployment cannot be masked by a proxy that only exists on a developer's machine.
// Set VITE_API_BASE_URL to talk to the API directly instead.
// `loadEnv` rather than `process.env` so this file needs no Node type definitions
// and reads the same .env files the app does. The third argument is the prefix
// filter: "" loads everything, including API_ORIGIN, which is not a VITE_ variable
// because it is only ever used here and must never be bundled into the client.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  return {
    plugins: [react(), tailwindcss()],
    server: {
      // Explicitly IPv4. Vite's default binds `localhost`, which resolves to ::1
      // first, and WSL2's localhost forwarding does not carry IPv6 -- so the dev
      // server is reachable inside the distro and invisible from Windows, which
      // looks exactly like the server having failed to start.
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: {
        "/api": {
          target: env.API_ORIGIN || "http://127.0.0.1:8000",
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ""),
        },
      },
    },
    build: {
      outDir: "dist",
      sourcemap: true,
    },
  };
});
