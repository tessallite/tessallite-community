export function formatThoughtText(text: string): string {
  return text
    .replace(/\\r\\n|\\n|\\r/g, "\n")
    .replace(/\r\n?/g, "\n")
    .replace(/\*\*([^*\n][^*\n]*?)\*\*/g, "$1")
    .replace(/__([^_\n][^_\n]*?)__/g, "$1")
    .replace(/`([^`\n]+?)`/g, "$1")
    .replace(/\*\*|__|`/g, "")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s*[-*]\s+/gm, "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .join("\n")
    .trim();
}
