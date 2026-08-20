/**
 * Local guard rules file for OpenClaw PDM Guard plugin.
 * Path default: ~/.openclaw/pdm-guard-rules.json
 */

import { createHash } from "node:crypto";
import { mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

export const RULES_FILE_VERSION = 1;
export const GUARD_RULE_MAX_LEN = 500;
export const DEFAULT_RULES_FILE = join(homedir(), ".openclaw", "pdm-guard-rules.json");

export interface GuardRuleEntry {
  id: string;
  text: string;
  created_at: string;
}

export interface GuardRulesFile {
  version: number;
  rules: GuardRuleEntry[];
}

export interface AddRuleResult {
  entry: GuardRuleEntry;
  created: boolean;
  deduped: boolean;
  file: GuardRulesFile;
}

export interface RemoveRuleResult {
  removed: GuardRuleEntry | null;
  file: GuardRulesFile;
}

let cachedPath: string | null = null;
let cachedMtimeMs = -1;
let cachedFile: GuardRulesFile | null = null;

export function expandHome(path: string): string {
  if (path === "~") return homedir();
  if (path.startsWith("~/")) return join(homedir(), path.slice(2));
  return path;
}

export function normalizeGuardRule(rule: unknown): string | null {
  if (rule == null) return null;
  const text = String(rule).trim().replace(/\s+/g, " ");
  if (!text) return null;
  return text.length > GUARD_RULE_MAX_LEN ? text.slice(0, GUARD_RULE_MAX_LEN) : text;
}

function emptyRulesFile(): GuardRulesFile {
  return { version: RULES_FILE_VERSION, rules: [] };
}

function ruleIdFor(text: string): string {
  return createHash("sha256").update(text).digest("hex").slice(0, 12);
}

function findRuleIndex(rules: GuardRuleEntry[], normalized: string): number {
  return rules.findIndex((entry) => normalizeGuardRule(entry.text) === normalized);
}

async function ensureParentDir(filePath: string): Promise<void> {
  await mkdir(dirname(filePath), { recursive: true });
}

async function writeRulesFileAtomic(filePath: string, file: GuardRulesFile): Promise<void> {
  await ensureParentDir(filePath);
  const tmpPath = `${filePath}.tmp`;
  const payload = `${JSON.stringify(file, null, 2)}\n`;
  await writeFile(tmpPath, payload, "utf8");
  await rename(tmpPath, filePath);
  invalidateRulesCache();
}

export function invalidateRulesCache(): void {
  cachedPath = null;
  cachedMtimeMs = -1;
  cachedFile = null;
}

export async function readRulesFile(filePath: string): Promise<GuardRulesFile> {
  const resolved = expandHome(filePath);
  try {
    const info = await stat(resolved);
    if (cachedPath === resolved && cachedFile && cachedMtimeMs === info.mtimeMs) {
      return cachedFile;
    }
    const raw = await readFile(resolved, "utf8");
    const parsed = JSON.parse(raw) as Partial<GuardRulesFile>;
    const file: GuardRulesFile = {
      version: typeof parsed.version === "number" ? parsed.version : RULES_FILE_VERSION,
      rules: Array.isArray(parsed.rules)
        ? parsed.rules
            .filter((entry): entry is GuardRuleEntry => {
              return (
                !!entry &&
                typeof entry === "object" &&
                typeof entry.text === "string" &&
                entry.text.trim().length > 0
              );
            })
            .map((entry) => ({
              id: typeof entry.id === "string" && entry.id ? entry.id : ruleIdFor(entry.text),
              text: normalizeGuardRule(entry.text) ?? entry.text.trim(),
              created_at:
                typeof entry.created_at === "string" && entry.created_at
                  ? entry.created_at
                  : new Date().toISOString(),
            }))
        : [],
    };
    cachedPath = resolved;
    cachedMtimeMs = info.mtimeMs;
    cachedFile = file;
    return file;
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code;
    if (code === "ENOENT") {
      const empty = emptyRulesFile();
      cachedPath = resolved;
      cachedMtimeMs = 0;
      cachedFile = empty;
      return empty;
    }
    throw err;
  }
}

export function goalTextsFromFile(file: GuardRulesFile): string[] {
  const seen = new Set<string>();
  const goals: string[] = [];
  for (const entry of file.rules) {
    const normalized = normalizeGuardRule(entry.text);
    if (!normalized || seen.has(normalized)) continue;
    seen.add(normalized);
    goals.push(normalized);
  }
  return goals;
}

export async function loadGoalTexts(filePath: string): Promise<string[]> {
  const file = await readRulesFile(filePath);
  return goalTextsFromFile(file);
}

export async function addGuardRule(
  filePath: string,
  rule: string,
): Promise<AddRuleResult> {
  const normalized = normalizeGuardRule(rule);
  if (!normalized) {
    throw new Error("rule must be a non-empty string");
  }

  const file = await readRulesFile(filePath);
  const existingIndex = findRuleIndex(file.rules, normalized);
  if (existingIndex >= 0) {
    const entry = file.rules[existingIndex];
    return { entry, created: false, deduped: true, file };
  }

  const entry: GuardRuleEntry = {
    id: ruleIdFor(normalized),
    text: normalized,
    created_at: new Date().toISOString(),
  };
  file.rules.push(entry);
  await writeRulesFileAtomic(expandHome(filePath), file);
  const saved = await readRulesFile(filePath);
  return {
    entry,
    created: true,
    deduped: false,
    file: saved,
  };
}

export async function removeGuardRule(
  filePath: string,
  ruleOrId: string,
): Promise<RemoveRuleResult> {
  const needle = normalizeGuardRule(ruleOrId) ?? ruleOrId.trim();
  if (!needle) {
    throw new Error("rule or id is required");
  }

  const file = await readRulesFile(filePath);
  const index = file.rules.findIndex((entry) => {
    if (entry.id === needle) return true;
    return normalizeGuardRule(entry.text) === needle;
  });

  if (index < 0) {
    return { removed: null, file };
  }

  const [removed] = file.rules.splice(index, 1);
  await writeRulesFileAtomic(expandHome(filePath), file);
  const saved = await readRulesFile(filePath);
  return { removed, file: saved };
}

export async function listGuardRules(filePath: string): Promise<GuardRulesFile> {
  return readRulesFile(filePath);
}
