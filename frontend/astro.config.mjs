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
    plugins: [
      {
        name: "songuess-dependency-cache",
        // Astro checks/builds and the live dev server must not overwrite one
        // another's optimized modules (including React's dev/prod runtimes).
        config: (_config, { command, mode }) => ({
          cacheDir: `node_modules/.vite/songuess-${command}-${mode}`,
        }),
      },
    ],
    optimizeDeps: {
      include: [
        "three",
        "three/addons/loaders/GLTFLoader.js",
        "three/addons/environments/RoomEnvironment.js",
        "three/addons/geometries/RoundedBoxGeometry.js",
      ],
    },
    server: {
      proxy: {
        "/api": { target: apiTarget, changeOrigin: false },
      },
    },
  },
});
