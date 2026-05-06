const MUTATING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
const HIGH_RISK_STEP_NAMES = [/delete/i, /void/i, /cancel/i, /remove/i, /replace/i, /apply/i];
const HIGH_RISK_PATHS = [/\/locations\//, /\/users\//, /\/snapshots/, /\/invoices\/.+\/void$/, /\/conversations\//];

export function evaluateApprovalRequirement({ step, mode, taskPack, event }) {
  const reasons = [];

  if (step.requiresApproval) reasons.push('step_marked_requires_approval');
  if (step.mutation) reasons.push('mutation_step');
  if (HIGH_RISK_STEP_NAMES.some((pattern) => pattern.test(step.name || ''))) reasons.push('high_risk_step_name');

  if (step.kind === 'adapter_call') {
    const inferredMethod = inferMethodFromStep(step);
    const inferredPath = inferPathFromStep(step);
    if (MUTATING_METHODS.has(inferredMethod)) reasons.push(`mutating_method:${inferredMethod}`);
    if (HIGH_RISK_PATHS.some((pattern) => pattern.test(inferredPath))) reasons.push('high_risk_endpoint');
  }

  if (taskPack?.approval_required && step.mutation) reasons.push('taskpack_requires_approval');
  if (mode === 'live' && step.mutation) reasons.push('live_mutation_gate');

  const requiresApproval = reasons.length > 0;
  return {
    requiresApproval,
    reason: requiresApproval ? reasons.join(',') : 'no_approval_required',
    reasons,
    eventType: event?.type || null,
    locationId: event?.locationId || null,
    companyId: event?.companyId || null
  };
}

function inferMethodFromStep(step) {
  const method = step.httpMethod || null;
  if (method) return method.toUpperCase();
  const name = `${step.method || ''}`.toLowerCase();
  if (name.startsWith('create') || name.startsWith('send') || name.startsWith('void')) return 'POST';
  if (name.startsWith('update')) return 'PUT';
  if (name.startsWith('delete')) return 'DELETE';
  return 'GET';
}

function inferPathFromStep(step) {
  return `${step.pathHint || ''}`;
}
