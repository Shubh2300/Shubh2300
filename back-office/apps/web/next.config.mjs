/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Kept dependency-light on purpose (office machine, fewer moving parts):
  // no eslint-config-next installed, so skip lint during `next build`.
  eslint: {
    ignoreDuringBuilds: true,
  },
};

export default nextConfig;
