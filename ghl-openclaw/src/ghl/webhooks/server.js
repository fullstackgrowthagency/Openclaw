import http from 'node:http';
import { getEnv } from '../../config/env.js';
import { normalizeWebhookEvent, verifyWebhookSignature, computePayloadHash } from './router.js';
import { WebhookStore } from './store.js';
import { WebhookProcessor } from './processor.js';

export class GhlWebhookServer {
  constructor({ store = new WebhookStore(), processor = new WebhookProcessor({ store }), host, port } = {}) {
    const env = getEnv();
    this.store = store;
    this.processor = processor;
    this.host = host || env.webhookHost;
    this.port = port || env.webhookPort;
    this.server = null;
  }

  async start() {
    if (this.server) return this.server;

    this.server = http.createServer((req, res) => {
      this.handleRequest(req, res).catch((error) => {
        res.writeHead(500, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: error.message }));
      });
    });

    await new Promise((resolve) => this.server.listen(this.port, this.host, resolve));
    return this.server;
  }

  async stop() {
    if (!this.server) return;
    await new Promise((resolve, reject) => this.server.close((error) => (error ? reject(error) : resolve())));
    this.server = null;
  }

  async handleRequest(req, res) {
    const url = new URL(req.url, `http://${req.headers.host || `${this.host}:${this.port}`}`);

    if (req.method === 'GET' && url.pathname === '/health') {
      const summary = await this.store.summary();
      return json(res, 200, { ok: true, status: 'healthy', summary });
    }

    if (req.method === 'GET' && url.pathname === '/webhooks/ghl/status') {
      const summary = await this.store.summary();
      return json(res, 200, { ok: true, summary });
    }

    if (req.method === 'POST' && url.pathname === '/webhooks/ghl') {
      const rawBody = await readRawBody(req);
      const verification = verifyWebhookSignature({ payload: rawBody, headers: req.headers });
      if (!verification.ok) {
        return json(res, 401, { ok: false, error: 'invalid_signature', reason: verification.reason });
      }

      let parsed;
      try {
        parsed = JSON.parse(rawBody);
      } catch {
        return json(res, 400, { ok: false, error: 'invalid_json' });
      }

      const normalizedEvent = normalizeWebhookEvent(parsed);
      const payloadHash = computePayloadHash(rawBody);
      const { record, duplicate } = await this.store.recordReceipt({
        normalizedEvent,
        payloadHash,
        verification,
        headers: req.headers
      });

      if (!duplicate) {
        await this.store.enqueueEvent(record);
        setImmediate(() => {
          this.processor.processNext().catch(() => {});
        });
      }

      return json(res, 200, {
        ok: true,
        duplicate: Boolean(duplicate),
        webhookId: normalizedEvent.webhookId,
        eventType: normalizedEvent.type
      });
    }

    return json(res, 404, { ok: false, error: 'not_found' });
  }
}

async function readRawBody(req) {
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks).toString('utf8');
}

function json(res, statusCode, body) {
  res.writeHead(statusCode, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
}
