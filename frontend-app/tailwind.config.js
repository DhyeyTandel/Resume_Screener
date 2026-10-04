/** Design tokens: one palette, used everywhere via these names. */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#f6f7f9",
        surface: "#ffffff",
        ink: "#16181d",
        muted: "#5c6470",
        line: "#e3e6ea",
        accent: { DEFAULT: "#2d5bd7", soft: "#eef2fb", ink: "#22366b" },
        ok: { DEFAULT: "#0f7b4f", soft: "#e6f4ec" },
        warn: { DEFAULT: "#8f5a00", soft: "#fdf2dd" },
        stop: { DEFAULT: "#a32b2b", soft: "#fbeaea" },
        neutral: { soft: "#eef1f6" },
      },
      boxShadow: { drawer: "0 0 40px rgba(10,12,18,.25)" },
    },
  },
  plugins: [],
};
