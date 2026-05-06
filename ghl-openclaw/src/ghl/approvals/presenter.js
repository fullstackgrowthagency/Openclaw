function minutesBetween(fromIso, toIso = new Date().toISOString()) {
  if (!fromIso) return null;
  const from = new Date(fromIso).getTime();
  const to = new Date(toIso).getTime();
  if (!Number.isFinite(from) || !Number.isFinite(to)) return null;
  return Math.max(0, Math.round((to - from) / 60000));
}

function humanizeReason(reason) {
  const labels = {
    step_marked_requires_approval: 'step explicitly marked for approval',
    mutation_step: 'step mutates data',
    taskpack_requires_approval: 'task pack requires approval for writes',
    live_mutation_gate: 'live write operations require approval',
    high_risk_step_name: 'step name indicates a high-risk action',
    high_risk_endpoint: 'endpoint is marked high risk'
  };

  if (!reason) return 'approval required';
  if (reason.startsWith('mutating_method:')) {
    return `HTTP ${reason.split(':')[1]} mutates data`;
  }
  return labels[reason] || reason.replaceAll('_', ' ');
}

function bucketAge(minutes) {
  if (minutes == null) return 'unknown';
  if (minutes < 15) return 'fresh';
  if (minutes < 60) return 'aging';
  if (minutes < 240) return 'stale';
  return 'critical';
}

function formatTarget(details = {}) {
  if (details.pathHint) return details.pathHint;
  if (details.plannedDetails?.action) return details.plannedDetails.action;
  if (details.action) return details.action;
  if (details.adapter && details.adapterMethod) return `${details.adapter}.${details.adapterMethod}`;
  if (details.stepKind) return details.stepKind;
  return 'workflow step';
}

function formatPlannedValue(value) {
  if (value == null) return null;
  if (Array.isArray(value)) return value.length ? value.join(', ') : null;
  if (typeof value === 'object') {
    const serialized = JSON.stringify(value);
    return serialized.length > 160 ? `${serialized.slice(0, 160)}...` : serialized;
  }
  const stringValue = String(value);
  return stringValue.length > 160 ? `${stringValue.slice(0, 160)}...` : stringValue;
}

export function enrichApproval(approval, nowIso = new Date().toISOString()) {
  const ageMinutes = minutesBetween(approval.createdAt, nowIso);
  const decisionLatencyMinutes = approval.decidedAt ? minutesBetween(approval.createdAt, approval.decidedAt) : null;
  const reasons = String(approval.reason || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);

  const reasonLabels = reasons.map(humanizeReason);
  const target = formatTarget(approval.details || {});
  const actionSummary = approval.details?.plannedDetails?.action
    || approval.details?.pathHint
    || approval.stepName;

  return {
    ...approval,
    ageMinutes,
    ageBucket: bucketAge(ageMinutes),
    decisionLatencyMinutes,
    reasonCodes: reasons,
    reasonLabels,
    riskLevel: approval.details?.riskLevel || 'standard',
    reviewHint: approval.details?.reviewHint || 'Review the planned action before deciding.',
    target,
    actionSummary,
    operatorSummary: `${approval.taskPackName}:${approval.stepName} on ${target}`
  };
}

export function summarizeApprovalMetrics(approvals, nowIso = new Date().toISOString()) {
  const enriched = approvals.map((approval) => enrichApproval(approval, nowIso));
  const pending = enriched.filter((item) => item.status === 'pending');
  const decided = enriched.filter((item) => item.status === 'approved' || item.status === 'rejected');
  const decisionLatencies = decided.map((item) => item.decisionLatencyMinutes).filter((value) => value != null);
  const oldestPendingMinutes = pending.reduce((max, item) => Math.max(max, item.ageMinutes || 0), 0);

  return {
    total: enriched.length,
    pending: pending.length,
    approved: enriched.filter((item) => item.status === 'approved').length,
    rejected: enriched.filter((item) => item.status === 'rejected').length,
    pendingOver15Min: pending.filter((item) => (item.ageMinutes || 0) >= 15).length,
    pendingOver60Min: pending.filter((item) => (item.ageMinutes || 0) >= 60).length,
    pendingOver240Min: pending.filter((item) => (item.ageMinutes || 0) >= 240).length,
    oldestPendingMinutes,
    avgDecisionMinutes: decisionLatencies.length
      ? Math.round(decisionLatencies.reduce((sum, value) => sum + value, 0) / decisionLatencies.length)
      : null,
    mostUrgent: pending
      .sort((a, b) => (b.ageMinutes || 0) - (a.ageMinutes || 0))
      .slice(0, 5)
      .map((item) => ({
        id: item.id,
        operatorSummary: item.operatorSummary,
        ageMinutes: item.ageMinutes,
        ageBucket: item.ageBucket,
        riskLevel: item.riskLevel,
        locationId: item.locationId || null,
        reasonLabels: item.reasonLabels,
        reviewHint: item.reviewHint
      }))
  };
}

export function buildApprovalStatusView(approvals, nowIso = new Date().toISOString()) {
  const enriched = approvals.map((approval) => enrichApproval(approval, nowIso));
  return {
    summary: summarizeApprovalMetrics(approvals, nowIso),
    approvals: enriched
  };
}

export function buildApprovalChatPrompt(approval, nowIso = new Date().toISOString()) {
  const enriched = enrichApproval(approval, nowIso);
  const planned = approval?.details?.plannedDetails || {};
  const lines = [
    `Approval needed: ${enriched.actionSummary}`,
    `ID: ${enriched.id}`,
    `Risk: ${enriched.riskLevel}`,
    `Target: ${enriched.target}`
  ];

  const fieldOrder = [
    ['contactName', 'Contact'],
    ['contactId', 'Contact ID'],
    ['conversationId', 'Conversation ID'],
    ['opportunityId', 'Opportunity ID'],
    ['workflowId', 'Workflow ID'],
    ['pipelineId', 'Pipeline ID'],
    ['pipelineStageId', 'Stage ID'],
    ['status', 'Status'],
    ['messageType', 'Message type'],
    ['channel', 'Channel'],
    ['fromNumber', 'From number'],
    ['toNumber', 'To number'],
    ['followers', 'Followers'],
    ['tags', 'Tags'],
    ['name', 'Name'],
    ['website', 'Website'],
    ['messagePreview', 'Preview']
  ];

  for (const [key, label] of fieldOrder) {
    const formatted = formatPlannedValue(planned[key]);
    if (formatted) {
      lines.push(`${label}: ${formatted}`);
    }
  }

  if (enriched.reviewHint) {
    lines.push(`Review: ${enriched.reviewHint}`);
  }

  lines.push(`Reply with approve ${enriched.id} or reject ${enriched.id}.`);

  return {
    id: enriched.id,
    title: `Approval needed: ${enriched.actionSummary}`,
    text: lines.join('\n'),
    suggestedReplies: [`approve ${enriched.id}`, `reject ${enriched.id}`],
    approval: enriched
  };
}
