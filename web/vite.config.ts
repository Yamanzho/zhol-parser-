import react from "@vitejs/plugin-react";
import { defineConfig, type ProxyOptions } from "vite";

const api = process.env.ZHOL_API_ORIGIN || "http://127.0.0.1:18080";

function localGate(): ProxyOptions["configure"] {
  return (proxy) => {
    proxy.on("proxyReq", (proxyReq) => {
      const gate = process.env.ZHOL_LOCAL_GATE || "";
      if (gate) proxyReq.setHeader("X-Zhol-Local-Gate", gate);
    });
  };
}

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: false,
    proxy: {
      "/api": { target: api, changeOrigin: true, configure: localGate() },
      "/health": { target: api, changeOrigin: true, configure: localGate() },
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": { target: api, changeOrigin: true, configure: localGate() },
      "/health": { target: api, changeOrigin: true, configure: localGate() },
    },
  },
});
