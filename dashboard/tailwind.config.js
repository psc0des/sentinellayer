/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      fontFamily: {
        // DM Sans — warmer, rounder than Inter. All UI text.
        sans: ['"DM Sans"', 'system-ui', 'sans-serif'],
        // JetBrains Mono — technical authority for all data values.
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
    },
  },
  plugins: [],
}
