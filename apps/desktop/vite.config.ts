import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";


export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { host: process.env.JANA_DEV_HOST ?? "localhost", port: Number(process.env.PORT ?? 5180), strictPort: true },
  build: {
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: "contracts", test: /contracts-ts[\\/]src[\\/]generated[\\/]client-validators\.js/ },
            { name: "editor", test: /node_modules[\\/](?:\.pnpm[\\/][^/]+[\\/]node_modules[\\/])?(?:@tiptap|prosemirror-)/ },
            { name: "maths", test: /node_modules[\\/](?:\.pnpm[\\/][^/]+[\\/]node_modules[\\/])?katex[\\/]/ },
          ],
        },
      },
    },
  },
  clearScreen: false,
});
