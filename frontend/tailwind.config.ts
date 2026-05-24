import type { Config } from 'tailwindcss';

/**
 * Tailwind CSS configuration with design system tokens.
 *
 * Two palettes coexist:
 *   - "brand" (sky-500 family) — legacy tokens used by /runs, /templates.
 *     Retained for backward compatibility; do NOT extend in new code.
 *   - "harness" — the canonical palette from docs/harness-redesign-2026-05-24/screens/*.svg.
 *     Two anchor tokens: purple (#7C5CFC) and teal (#2DD4BF). Surface scale
 *     anchored to #0B0D10. Status colors match the SVG palette exactly.
 *
 * All new harness code uses the harness palette. Legacy pages migrate over
 * time; we do not gut them in the same diff that introduces the shell.
 */
const config: Config = {
  content: [
    './pages/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
    './app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // ── Legacy brand (kept for existing /runs, /templates pages) ─────────
        brand: {
          50: '#f0f9ff',
          100: '#e0f2fe',
          200: '#bae6fd',
          300: '#7dd3fc',
          400: '#38bdf8',
          500: '#0ea5e9',
          600: '#0284c7',
          700: '#0369a1',
          800: '#075985',
          900: '#0c4a6e',
          950: '#082f49',
        },
        // ── Harness palette (matches SVG canon exactly) ──────────────────────
        harness: {
          // Surfaces (background → elevated)
          bg: '#0B0D10',         // deepest background — matches SVG `<rect fill>`
          surface: '#14171C',    // panel base
          surface2: '#1B1F26',   // elevated card top
          surface3: '#161A21',   // alt elevated
          border: '#20242C',     // 1px borders
          borderStrong: '#2A2F38',
          // Content
          text: '#E8ECF3',       // primary
          muted: '#8A93A2',      // secondary
          dim: '#5A6371',        // tertiary
          dimmer: '#404853',
          // Brand
          purple: '#7C5CFC',
          purpleDim: '#5A40C7',
          teal: '#2DD4BF',
          tealDim: '#1FA791',
          // Status
          success: '#2DD4BF',
          warning: '#F59E0B',
          danger: '#EF4444',
          info: '#7C5CFC',
          // Tinted surfaces (status backgrounds)
          successBg: '#1B2A1F',
          warningBg: '#3B2E1F',
          dangerBg: '#2A1F1F',
          infoBg: '#1F1F2E',
          drafterBg: '#1F1B2E',
          reviewerBg: '#1A2A28',
        },
        // Legacy surface tokens (unchanged for /runs, /templates)
        surface: {
          0: '#09090b',
          1: '#18181b',
          2: '#27272a',
          3: '#3f3f46',
          4: '#52525b',
        },
        content: {
          primary: '#fafafa',
          secondary: '#a1a1aa',
          tertiary: '#71717a',
          inverse: '#09090b',
        },
        success: '#22c55e',
        warning: '#f59e0b',
        error: '#ef4444',
        info: '#3b82f6',
      },
      fontFamily: {
        sans: ['var(--font-geist-sans)', 'system-ui', 'sans-serif'],
        mono: ['var(--font-geist-mono)', 'monospace'],
      },
      borderRadius: {
        sm: '0.25rem',
        md: '0.375rem',
        lg: '0.5rem',
        xl: '0.75rem',
        '2xl': '1rem',
      },
      spacing: {
        '4.5': '1.125rem',
        '13': '3.25rem',
        '18': '4.5rem',
        '60': '15rem',  // 240px — left rail width
      },
      backgroundImage: {
        'harness-brand': 'linear-gradient(90deg, #7C5CFC 0%, #2DD4BF 100%)',
        'harness-card': 'linear-gradient(180deg, #1B1F26 0%, #14171C 100%)',
        'harness-grid':
          "url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='40' height='40' viewBox='0 0 40 40'><path d='M 40 0 L 0 0 0 40' fill='none' stroke='%231A1E25' stroke-width='0.5'/></svg>\")",
      },
      keyframes: {
        'pulse-soft': {
          '0%, 100%': { opacity: '0.4' },
          '50%': { opacity: '1' },
        },
      },
      animation: {
        'pulse-soft': 'pulse-soft 1.6s ease-in-out infinite',
      },
    },
  },
  plugins: [],
};

export default config;
