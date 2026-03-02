import type { Config } from 'tailwindcss';

/**
 * Tailwind CSS configuration with design system tokens.
 * Dark theme first — all tokens are defined for dark backgrounds.
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
        // Brand palette
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
        // Surface tokens (dark theme)
        surface: {
          0: '#09090b',   // deepest background (zinc-950)
          1: '#18181b',   // primary background (zinc-900)
          2: '#27272a',   // card / elevated surface (zinc-800)
          3: '#3f3f46',   // border / divider (zinc-700)
          4: '#52525b',   // subtle (zinc-600)
        },
        // Content tokens
        content: {
          primary: '#fafafa',    // headings, primary text (zinc-50)
          secondary: '#a1a1aa',  // secondary / muted text (zinc-400)
          tertiary: '#71717a',   // placeholder / disabled (zinc-500)
          inverse: '#09090b',    // text on light backgrounds
        },
        // Status tokens
        success: '#22c55e',  // green-500
        warning: '#f59e0b',  // amber-500
        error: '#ef4444',    // red-500
        info: '#3b82f6',     // blue-500
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
        // Semantic spacing scale
        '4.5': '1.125rem',
        '13': '3.25rem',
        '18': '4.5rem',
      },
    },
  },
  plugins: [],
};

export default config;
