const DESTRUCTIVE_METHODS = new Set(['DELETE']);
const HIGH_RISK_PATTERNS = [/\/invoices\/.+\/void$/, /\/locations\//, /\/snapshots/, /\/users\//];

export function evaluatePolicy({ agentId, assignedLocationId, capability, targetLocationId, bulkCount = 1, approvalBulkThreshold = 25 }) {
  if (!capability) {
    return deny('capability_missing');
  }

  if (capability.status === 'not_publicly_api_supported') {
    return deny('not_publicly_api_supported');
  }

  if (assignedLocationId && targetLocationId && assignedLocationId !== targetLocationId) {
    return deny('cross_location_access_attempt');
  }

  const requiresApproval =
    DESTRUCTIVE_METHODS.has(capability.method) ||
    HIGH_RISK_PATTERNS.some((pattern) => pattern.test(capability.path)) ||
    bulkCount > approvalBulkThreshold;

  return {
    allowed: true,
    agentId,
    targetLocationId: targetLocationId || null,
    requiresApproval,
    reason: requiresApproval ? 'approval_required' : 'allowed'
  };
}

function deny(reason) {
  return {
    allowed: false,
    requiresApproval: false,
    reason
  };
}
