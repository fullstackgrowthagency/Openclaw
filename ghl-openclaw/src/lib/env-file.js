import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

let loaded = false;

export function loadDotEnv(cwd = process.cwd()) {
  if (loaded) return;

  const envPath = path.join(cwd, '.env');
  if (!existsSync(envPath)) {
    loaded = true;
    return;
  }

  const raw = readFileSync(envPath, 'utf8');
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const idx = trimmed.indexOf('=');
    if (idx === -1) continue;
    const key = trimmed.slice(0, idx).trim();
    let value = trimmed.slice(idx + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    if (!(key in process.env)) {
      process.env[key] = value;
    }
  }

  loaded = true;
}
