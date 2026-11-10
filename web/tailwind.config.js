// Design tokens. The look follows a calm "steel blue on white" scheme: a light sidebar, a white
// canvas, thin borders and ~8-12px rounded corners. Use `brand-*` for the accent, `canvas` and
// `sidebar` for surfaces; everything else stays on Tailwind's slate scale.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#eef6fa",
          100: "#dbeaf2",
          200: "#b7d5e5",
          500: "#3b86a8",
          600: "#2c6f8f",
          700: "#255c77",
          800: "#1f4b61",
        },
        canvas: "#fbfcfd",
        sidebar: "#f4f6f8",
      },
      fontFamily: {
        sans: ['"Inter"', "ui-sans-serif", "system-ui", "-apple-system", '"Segoe UI"', "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      borderRadius: { xl: "0.75rem" },
      boxShadow: { card: "0 1px 2px rgba(16,24,40,0.04), 0 1px 3px rgba(16,24,40,0.06)" },
    },
  },
  plugins: [],
};
