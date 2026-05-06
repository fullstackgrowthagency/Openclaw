import path from 'node:path';
import crypto from 'node:crypto';
import { getEnv } from '../../config/env.js';
import { readJson, writeJson } from '../../lib/fs.js';

function nowIso() {
  return new Date().toISOString();
}

function createApprovalId() {
  return `apr_${crypto.randomUUID()}`;
}

function initialState() {
  return { approvals: [] };
}

export class ApprovalStore {
  constructor() {
    const env = getEnv();
    this.filePath = path.join(env.dataDir, 'approvals', 'requests.json');
  }

  async load() {
    return (await readJson(this.filePath, initialState())) || initialState();
  }

  async save(state) {
    await writeJson(this.filePath, state);
  }

  async createRequest({
    runId,
    queueId = null,
    taskPackName,
    stepName,
    agentId,
    companyId = null,
    locationId = null,
    eventType,
    mode,
    reason,
    details = null,
    key = null
  }) {
    const state = await this.load();
    const dedupeKey = key || `${runId}:${stepName}:${reason}`;
    const existing = state.approvals.find((item) => item.dedupeKey === dedupeKey && item.status === 'pending');
    if (existing) return existing;

    const request = {
      id: createApprovalId(),
      dedupeKey,
      runId,
      queueId,
      taskPackName,
      stepName,
      agentId,
      companyId,
      locationId,
      eventType,
      mode,
      reason,
      details,
      status: 'pending',
      createdAt: nowIso(),
      updatedAt: nowIso(),
      decidedAt: null,
      decider: null,
      decisionNote: null
    };

    state.approvals.push(request);
    await this.save(state);
    return request;
  }

  async list(limit = 50, status = null) {
    const state = await this.load();
    let items = state.approvals;
    if (status) items = items.filter((item) => item.status === status);
    return items.slice(-limit).reverse();
  }

  async get(id) {
    const state = await this.load();
    return state.approvals.find((item) => item.id === id) || null;
  }

  async decide(id, { decision, decider = 'human_admin', note = null }) {
    const state = await this.load();
    const approval = state.approvals.find((item) => item.id === id);
    if (!approval) return null;
    if (!['approved', 'rejected'].includes(decision)) {
      throw new Error(`Invalid approval decision: ${decision}`);
    }

    approval.status = decision;
    approval.decider = decider;
    approval.decisionNote = note;
    approval.decidedAt = nowIso();
    approval.updatedAt = nowIso();
    await this.save(state);
    return approval;
  }

  async summary() {
    const state = await this.load();
    return {
      total: state.approvals.length,
      pending: state.approvals.filter((item) => item.status === 'pending').length,
      approved: state.approvals.filter((item) => item.status === 'approved').length,
      rejected: state.approvals.filter((item) => item.status === 'rejected').length
    };
  }
}
