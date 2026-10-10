import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: `npm run dev` on :5173 proxies /v1 to `cyp serve` on :8765.
// Prod: `npm run build` -> dist/, served by `cyp serve` at "/".
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/v1": "http://127.0.0.1:8765" } },
  build: { outDir: "dist", emptyOutDir: true },
});
