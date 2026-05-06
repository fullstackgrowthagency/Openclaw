import { sha256 } from '../../lib/hash.js';

export async function fetchDoc(url) {
  const response = await fetch(url, {
    headers: {
      'user-agent': 'OpenClaw-GHL-Builder/0.1'
    }
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch ${url}: ${response.status}`);
  }

  const text = await response.text();
  return {
    url,
    fetchedAt: new Date().toISOString(),
    statusCode: response.status,
    body: text,
    fingerprint: sha256(text)
  };
}
