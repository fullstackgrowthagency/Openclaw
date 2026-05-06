import path from 'node:path';

export function getEnv() {
  const cwd = process.cwd();
  const dataDir = path.resolve(cwd, process.env.GHL_DATA_DIR || './data');

  return {
    cwd,
    dataDir,
    docsBaseUrl: process.env.GHL_DOCS_BASE_URL || 'https://marketplace.gohighlevel.com/docs',
    clientId: process.env.GHL_CLIENT_ID || null,
    clientSecret: process.env.GHL_CLIENT_SECRET || null,
    redirectUri: process.env.GHL_REDIRECT_URI || null,
    webhookPublicKey: process.env.GHL_WEBHOOK_PUBLIC_KEY || null,
    agencyPit: process.env.GHL_AGENCY_PIT || null,
    docsRefreshHours: Number(process.env.GHL_DOCS_REFRESH_HOURS || 12),
    approvalBulkThreshold: Number(process.env.GHL_APPROVAL_BULK_THRESHOLD || 25)
  };
}
