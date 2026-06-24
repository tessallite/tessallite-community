type AxiosLikeError = {
  response?: {
    data?: {
      detail?:
        | string
        | Array<{ msg?: string; loc?: unknown[] }>
        | { message?: string; error?: string };
    };
  };
  message?: string;
};

export function extractApiError(err: unknown, fallback: string): string {
  const e = err as AxiosLikeError;
  const detail = e?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    return detail.map((d) => d.msg ?? JSON.stringify(d)).join("; ");
  }
  if (detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string") {
    return detail.message;
  }
  if (typeof e?.message === "string" && e.message !== "Network Error") {
    return e.message;
  }
  return fallback;
}
