/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',
  images: {
    unoptimized: true,
  },
  // Dev-only rewrites. With `output: 'export'`, rewrites are dropped from
  // the production build (Next prints a warning at build time, expected).
  // In `next dev` they apply normally.
  //
  // Two rules:
  //   1. /api/*    → forward to the FastAPI engine on port 8374
  //   2. /runs/:id and /templates/:id → internally serve the `_`
  //      static-params page with the real id moved to a query param.
  //      Without this, `next dev` 500s on any dynamic id not enumerated
  //      in generateStaticParams(). In production the engine's static
  //      file server already does the equivalent SPA-fallback rewrite.
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: 'http://localhost:8374/api/:path*',
      },
      // Specific id segments — avoid catching /runs and /runs/_.
      {
        source: '/runs/:id((?!_$).+)',
        destination: '/runs/_?run=:id',
      },
      {
        source: '/templates/:id((?!_$|new$).+)',
        destination: '/templates/_?id=:id',
      },
    ];
  },
};

module.exports = nextConfig;
