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
  // Static export: produces frontend/out/ for FastAPI to serve.
  // All pages are "use client" with client-side data fetching, so static
  // export works perfectly — each dynamic route generates a shell HTML that
  // hydrates client-side.
  output: "export",

  // Images: disable optimisation API (served by FastAPI, not Next.js server)
  images: {
    unoptimized: true,
  },

  // Rewrites proxy /api/* to FastAPI during development only.
  // NOTE: rewrites are ignored when output="export", but kept for `next dev`.
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
