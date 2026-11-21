import { defineConfig } from 'vite';

export default defineConfig({
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        configure(proxy) {
          proxy.on('error', (_error, _request, response) => {
            if (typeof response.writeHead !== 'function' || response.headersSent || response.writableEnded) return;
            response.writeHead(503, { 'Content-Type': 'application/json; charset=utf-8' });
            response.end(JSON.stringify({ error: { code: 'API_UNREACHABLE' } }));
          });
        },
      },
    },
  },
});
