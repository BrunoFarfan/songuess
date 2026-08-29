import { defineConfig } from "astro/config";
import react from "@astrojs/react";

const apiTarget = process.env.SONGUESS_API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  site: "https://songuess.bruni.to",
  integrations: [react()],
  output: "static",
  devToolbar: {
    enabled: false,
  },
  vite: {
    server: {
      proxy: {
        "/api": apiTarget,
      },
    },
  },
});
