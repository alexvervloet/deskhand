/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Empty in production, where FastAPI serves these assets itself. */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
