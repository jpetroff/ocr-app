import { defineConfig } from 'vite';

const root = import.meta.dirname;

export default defineConfig({
  root,
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
    fs: {
      strict: true,
      allow: [root],
    },
  },
});
