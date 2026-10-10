import type { NextConfig } from "next";

const config: NextConfig = {
  basePath: "/app",
  poweredByHeader: false,
  reactStrictMode: true,
  productionBrowserSourceMaps: false,
  // Authentication, uploads and data remain on the Python origin through Nginx.
  // Do not add a rewrites proxy that forwards cookies to a configurable host.
};
export default config;
