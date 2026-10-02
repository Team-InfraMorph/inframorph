import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 개발 중에는 5173 화면이 /api 요청을 8000 Control Plane으로 넘긴다.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8000" } },
});
