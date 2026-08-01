/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "1" → Flow-only deployment (see store/appMode FLOW_ONLY). */
  readonly VITE_FLOW_ONLY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
