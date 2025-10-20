// frontend/vite.config.ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  envDir: "..",                      // .env en la raíz del repo
  // server: { host: true, port: 5173 },// útil en Docker/WSL
  server: {
    host: true,
    port: 8080,
    proxy: {
      "^/api": {
        target: "http://localhost:8000", // puerto real del backend FastAPI
        changeOrigin: true,
        secure: false,
      },
    },
  },
  preview: { host: true, port: 8080 },
  build: {
    target: "es2020",
    sourcemap: true,                 // ayuda a depurar pantallas blancas
  },
});