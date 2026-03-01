/** @type {import('next').NextConfig} */

/**
 * Next.js configuration for the Orchestration Engine UI.
 *
 * In development (`next dev`), API requests to /api/* are proxied to the
 * FastAPI backend running on port 8374 (`orch serve`).
 *
 * In production, `next build` produces a standalone Node.js server or
 * static assets. The FastAPI server (`orch serve`) serves the built
 * frontend and handles SPA fallback routing.
 */
const nextConfig = {
  // Images: disable optimisation API (served by FastAPI, not Next.js server)
  images: {
    unoptimized: true,
  },

  // Rewrites proxy /api/* to FastAPI during development only.
  ...(process.env.NODE_ENV === 'development' ? {
    async rewrites() {
      return [
        {
          source: "/api/:path*",
          destination: "http://localhost:8374/api/:path*",
        },
      ];
    },
  } : {}),
};

module.exports = nextConfig;
