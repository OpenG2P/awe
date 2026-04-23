import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The bundled SPA is served by the FastAPI app under /v1/awe/admin/.
// The Helm chart's Istio VirtualService routes /v1/awe/* to this service so
// the SPA's assets resolve correctly behind Istio without rewrite rules.
export default defineConfig({
  plugins: [react()],
  base: "/v1/awe/admin/",
  build: {
    outDir: "../src/awe/admin_ui/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      // Proxy API calls to the FastAPI backend, but let Vite serve the SPA
      // itself at /v1/awe/admin/* — otherwise the page load gets forwarded
      // upstream and returns the API's 404.
      "/v1/awe": {
        target: "http://localhost:8000",
        changeOrigin: true,
        bypass: (req) => {
          if (req.url?.startsWith("/v1/awe/admin")) {
            return req.url;
          }
        },
      },
    },
  },
});
