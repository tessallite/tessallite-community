const UNITS: [string, number][] = [
  ["year", 31536000],
  ["month", 2592000],
  ["week", 604800],
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60],
];

export function formatRelativeTime(dateStr: string | null | undefined): string {
  if (!dateStr) return "";
  const date = new Date(dateStr);
  const now = new Date();
  const diff = Math.floor((now.getTime() - date.getTime()) / 1000);

  if (Number.isNaN(date.getTime())) return "";
  if (diff < 10) return "Just now";
  if (diff < 60) return `${diff}s ago`;

  for (const [unit, seconds] of UNITS) {
    const val = Math.floor(diff / seconds);
    if (val >= 1) {
      return `${val} ${unit}${val > 1 ? "s" : ""} ago`;
    }
  }

  return date.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

export function truncate(str: string, maxLen: number): string {
  if (str.length <= maxLen) return str;
  return str.slice(0, maxLen).trimEnd() + "...";
}
