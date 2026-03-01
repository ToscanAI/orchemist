import type { Config } from "tailwindcss";

/**
 * Tailwind CSS configuration with a dark-first design system.
 *
 * Colour tokens follow a consistent naming scheme:
 *   - surface-*  : background layers (canvas, card, elevated)
 *   - border-*   : dividers and ring colours
 *   - text-*     : typography hierarchy
 *   - accent-*   : brand / interactive colours
 *   - status-*   : semantic state colours (success, warning, error)
 */
const config: Config = {
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Surface layers (dark theme defaults)
        surface: {
          canvas: "#0d1117",     // Page background
          card: "#161b22",       // Card / panel background
          elevated: "#21262d",   // Elevated / hover surface
          overlay: "#30363d",    // Modal / popover background
        },
        // Border colours
        border: {
          DEFAULT: "#30363d",
          subtle: "#21262d",
          emphasis: "#484f58",
        },
        // Text hierarchy
        text: {
          primary: "#e6edf3",
          secondary: "#8b949e",
          muted: "#6e7681",
          inverse: "#0d1117",
        },
        // Accent / brand (blue)
        accent: {
          DEFAULT: "#388bfd",
          hover: "#58a6ff",
          muted: "#1f6feb",
        },
        // Semantic status colours
        status: {
          success: "#3fb950",
          warning: "#d29922",
          error: "#f85149",
          info: "#388bfd",
          running: "#58a6ff",
          paused: "#d29922",
          cancelled: "#6e7681",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "monospace",
        ],
      },
      borderRadius: {
        sm: "4px",
        DEFAULT: "6px",
        md: "8px",
        lg: "12px",
        xl: "16px",
      },
      boxShadow: {
        card: "0 0 0 1px #30363d",
        "card-hover": "0 0 0 1px #484f58",
        overlay: "0 8px 24px rgba(1, 4, 9, 0.5)",
      },
      animation: {
        "spin-slow": "spin 2s linear infinite",
        "pulse-soft": "pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite",
      },
    },
  },
  plugins: [],
};

export default config;
