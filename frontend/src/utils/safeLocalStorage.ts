export function safeLocalGet(key: string, fallback: string): string {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    console.warn(`Could not read localStorage key "${key}", using default.`);
    return fallback;
  }
}

export function safeLocalSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    console.warn(`Could not write localStorage key "${key}".`);
  }
}

export function safeLocalRemove(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    console.warn(`Could not remove localStorage key "${key}".`);
  }
}

export function safeLocalGetJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    console.warn(`Corrupted localStorage key "${key}", using default.`);
    return fallback;
  }
}
