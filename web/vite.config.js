import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
/** 开发时将 API 请求转给本机 FastAPI，避免在应用代码中放宽 CORS。 */
export default defineConfig({
    plugins: [react()],
    server: {
        port: 5173,
        proxy: {
            "/api": "http://127.0.0.1:8000",
            "/health": "http://127.0.0.1:8000",
        },
    },
});
