/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Tessallite version shown in the login about line (Bug-9558), e.g.
   * "1.1.6". Supplied by the build environment (VITE_TESSALLITE_VERSION).
   * Unset builds render the version as "unknown".
   */
  readonly VITE_TESSALLITE_VERSION?: string;
  /**
   * Deployment type shown in the login about line: "Dev Stack" |
   * "Community Edition" | "Enterprise Edition" | "Cloud Edition".
   * Unset builds omit the type portion.
   */
  readonly VITE_DEPLOYMENT_TYPE?: string;
  /**
   * Git commit hash of the build. The login about line displays its first
   * 13 characters (operator's example format [9138721837AH5]). Unset builds
   * omit the hash portion.
   */
  readonly VITE_BUILD_COMMIT_HASH?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
