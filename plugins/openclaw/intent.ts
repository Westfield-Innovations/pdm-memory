/**
 * Build a short, deterministic intent string for pdm_memory.verify().
 *
 * Do not emit a `file=` field label — verify() tokenizes labels, and the bare
 * word "file" false-triggers rules like "never create a file named …" for
 * every write. Prefer `path=<basename-or-path>` so only the real name matters.
 */

const PATH_KEYS = ["path", "file", "filename", "file_path", "filepath", "target"] as const;
const COMMAND_KEYS = ["command", "cmd"] as const;
const CONTENT_KEYS = ["content", "text", "patch", "script", "input"] as const;

const CONTENT_PREVIEW_MAX = 120;
const COMMAND_MAX = 800;
const INTENT_MAX = 1000;

function asNonEmptyString(value: unknown): string {
  if (typeof value !== "string") return "";
  const trimmed = value.trim();
  return trimmed ? trimmed : "";
}

function basename(path: string): string {
  const normalized = path.replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  return parts[parts.length - 1] || path;
}

function firstString(
  params: Record<string, unknown>,
  keys: readonly string[],
): string {
  for (const key of keys) {
    const value = asNonEmptyString(params[key]);
    if (value) return value;
  }
  return "";
}

function clip(value: string, maxLen: number): string {
  if (value.length <= maxLen) return value;
  return `${value.slice(0, Math.max(0, maxLen - 1))}…`;
}

/**
 * Compact tool intent for the GAA gate.
 *
 * Examples:
 *   write path=config.py content≈DB_HOST=…
 *   exec command=curl http://localhost:8080/api/health
 */
export function buildIntentText(
  toolName: string,
  params: Record<string, unknown> = {},
): string {
  const tool = asNonEmptyString(toolName) || "unknown_tool";
  const parts: string[] = [tool];

  const path = firstString(params, PATH_KEYS);
  if (path) {
    // Basename only — long absolute paths dilute verify() resonance and can
    // fail-open a real ban. Never emit a `file=` label (token "file" false-hits
    // rules like "never create a file named …").
    parts.push(`path=${basename(path)}`);
  }

  const command = firstString(params, COMMAND_KEYS);
  if (command) {
    parts.push(`command=${clip(command, COMMAND_MAX)}`);
  } else {
    const content = firstString(params, CONTENT_KEYS);
    if (content) {
      parts.push(`content≈${clip(content, CONTENT_PREVIEW_MAX)}`);
    }
  }

  if (parts.length === 1) {
    const leftovers = Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== null && v !== "")
      .slice(0, 6)
      .map(([k, v]) => {
        try {
          const raw = typeof v === "string" ? v : JSON.stringify(v);
          return `${k}=${clip(raw, 80)}`;
        } catch {
          return `${k}=${clip(String(v), 80)}`;
        }
      });
    if (leftovers.length) {
      parts.push(...leftovers);
    }
  }

  return clip(parts.join(" "), INTENT_MAX);
}
