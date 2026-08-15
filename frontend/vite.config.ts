import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the API runs on :8000 and Vite serves the UI on :5173.
// In production the built assets are served by FastAPI itself, so requests are
// same-origin and VITE_API_BASE is empty.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  build: { outDir: "dist" },
});
