/** @type {import('next').NextConfig} */

/**
 * Next.js configuration for the Orchestration Engine UI.
 *
 * In development (`next dev`), API requests to /api/* are proxied to the
 * FastAPI backend running on port 8374 (`orch serve`).
 *
 * In production, `next build && next export` produces a static HTML/CSS/JS
 * bundle in `frontend/out/` that is served directly by `orch serve` via
 * FastAPI's StaticFiles mount.
 */
const nextConfig = {
  // Enable static HTML export for `npm run export`
  output: "export",

  // Required when using `next export` — disables image optimisation API
  // (not available in static builds).
  images: {
    unoptimized: true,
  },

  // Rewrites only apply in `next dev` / `next start` (not static export).
  // They proxy /api/* requests to the FastAPI backend during development.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:8374/api/:path*",
      },
    ];
  },
};

module.exports = nextConfig;
