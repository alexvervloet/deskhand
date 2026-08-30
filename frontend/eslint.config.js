import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules"] },
  {
    files: ["src/**/*.{ts,tsx}"],
    extends: [
      js.configs.recommended,
      // Type-aware, because the rules worth having here (floating promises,
      // misused awaits) need the checker to say anything at all.
      ...tseslint.configs.recommendedTypeChecked,
      // Not configs["recommended-latest"], which is still the eslintrc shape
      // in v7 and makes ESLint 10 reject the whole file.
      reactHooks.configs.flat["recommended-latest"],
    ],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: { "react-refresh": reactRefresh },
    rules: {
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // tsc already reports unused locals and parameters, and two tools
      // reporting the same line twice trains people to skim both.
      "@typescript-eslint/no-unused-vars": "off",
    },
  },
);
