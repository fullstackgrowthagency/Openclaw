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
    riskLevel: inferRiskLevel(reasons),
    reviewHint: buildReviewHint(reasons),
    eventType: event?.type || null,
    locationId: event?.locationId || null,
    companyId: event?.companyId || null
  };
}

function inferRiskLevel(reasons) {
  if (reasons.includes('high_risk_endpoint') || reasons.includes('high_risk_step_name')) return 'high';
  if (reasons.includes('live_mutation_gate')) return 'high';
  if (reasons.length >= 3) return 'medium';
  return 'standard';
}

function buildReviewHint(reasons) {
  if (reasons.includes('high_risk_endpoint')) return 'Verify the exact target resource before approving.';
  if (reasons.includes('live_mutation_gate')) return 'This will perform a live write when credentials are present.';
  if (reasons.includes('taskpack_requires_approval')) return 'Task-pack policy requires a human decision before this write step.';
  if (reasons.includes('mutation_step')) return 'Confirm the intended write action still matches the operator goal.';
  return 'Review the planned action before deciding.';
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
