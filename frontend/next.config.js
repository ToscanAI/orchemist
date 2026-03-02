/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',
  images: {
    unoptimized: true,
  },
  // Dev proxy: /api/* → FastAPI on port 8374
  // Note: rewrites are ignored when output: 'export' is set.
  // They only apply during `next dev` (development mode).
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: 'http://localhost:8374/api/:path*',
      },
    ];
  },
};

module.exports = nextConfig;
