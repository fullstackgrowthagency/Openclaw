import path from 'node:path';
import crypto from 'node:crypto';
import { getEnv } from '../../config/env.js';
import { readJson, writeJson } from '../../lib/fs.js';

function nowIso() {
  return new Date().toISOString();
}

function createRunId() {
  return `run_${crypto.randomUUID()}`;
}

function initialState() {
  return { runs: [] };
}

export class TaskPackRunStore {
  constructor() {
    const env = getEnv();
    this.filePath = path.join(env.dataDir, 'taskpacks', 'runs.json');
  }

  async load() {
    return (await readJson(this.filePath, initialState())) || initialState();
  }

  async save(state) {
    await writeJson(this.filePath, state);
  }

  async createRun({ taskPackName, agentId, event, mode, credentialRef = null, queueId = null }) {
    const state = await this.load();
    const run = {
      id: createRunId(),
      taskPackName,
      agentId,
      queueId,
      eventType: event.type,
      companyId: event.companyId || null,
      locationId: event.locationId || null,
      objectId: event.objectId || event?.payload?.data?.id || event?.payload?.id || null,
      event,
      mode,
      credentialRef,
      status: 'running',
      createdAt: nowIso(),
      updatedAt: nowIso(),
      steps: [],
      summary: null,
      error: null
    };
    state.runs.push(run);
    await this.save(state);
    return run;
  }

  async getRun(runId) {
    const state = await this.load();
    return state.runs.find((item) => item.id === runId) || null;
  }

  async setStep(runId, stepIndex, step) {
    const state = await this.load();
    const run = state.runs.find((item) => item.id === runId);
    if (!run) return null;
    run.steps[stepIndex] = step;
    run.updatedAt = nowIso();
    await this.save(state);
    return run;
  }

  async appendStep(runId, step) {
    const state = await this.load();
    const run = state.runs.find((item) => item.id === runId);
    if (!run) return null;
    run.steps.push(step);
    run.updatedAt = nowIso();
    await this.save(state);
    return run;
  }

  async setStatus(runId, status, { summary = undefined, error = undefined } = {}) {
    const state = await this.load();
    const run = state.runs.find((item) => item.id === runId);
    if (!run) return null;
    run.status = status;
    if (summary !== undefined) run.summary = summary;
    if (error !== undefined) run.error = error;
    run.updatedAt = nowIso();
    await this.save(state);
    return run;
  }

  async completeRun(runId, summary) {
    return this.setStatus(runId, 'completed', { summary, error: null });
  }

  async failRun(runId, error) {
    return this.setStatus(runId, 'failed', { error });
  }

  async listRuns(limit = 20) {
    const state = await this.load();
    return state.runs.slice(-limit).reverse();
  }

  async summary() {
    const runs = await this.listRuns(500);
    return {
      totalRuns: runs.length,
      completedRuns: runs.filter((item) => item.status === 'completed').length,
      failedRuns: runs.filter((item) => item.status === 'failed').length,
      runningRuns: runs.filter((item) => item.status === 'running').length,
      awaitingApprovalRuns: runs.filter((item) => item.status === 'awaiting_approval').length,
      rejectedRuns: runs.filter((item) => item.status === 'rejected').length,
      dryRunCount: runs.filter((item) => item.mode === 'dry_run').length,
      liveRunCount: runs.filter((item) => item.mode === 'live').length
    };
  }
}
