import crypto from 'node:crypto';
import path from 'node:path';
import { getEnv } from '../../config/env.js';
import { readJson, writeJson } from '../../lib/fs.js';

function deriveKey(secret) {
  return crypto.createHash('sha256').update(secret).digest();
}

function encryptJson(data, secret) {
  const iv = crypto.randomBytes(12);
  const key = deriveKey(secret);
  const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
  const plaintext = Buffer.from(JSON.stringify(data), 'utf8');
  const encrypted = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  const tag = cipher.getAuthTag();
  return {
    iv: iv.toString('base64'),
    tag: tag.toString('base64'),
    ciphertext: encrypted.toString('base64')
  };
}

function decryptJson(record, secret) {
  const key = deriveKey(secret);
  const decipher = crypto.createDecipheriv('aes-256-gcm', key, Buffer.from(record.iv, 'base64'));
  decipher.setAuthTag(Buffer.from(record.tag, 'base64'));
  const plaintext = Buffer.concat([
    decipher.update(Buffer.from(record.ciphertext, 'base64')),
    decipher.final()
  ]);
  return JSON.parse(plaintext.toString('utf8'));
}

export class FileEncryptedStore {
  constructor() {
    const env = getEnv();
    this.secret = env.secretKey;
    this.filePath = path.join(env.dataDir, 'secrets', 'credentials.json');
  }

  assertReady() {
    if (!this.secret) {
      throw new Error('GHL_SECRET_KEY is required for encrypted credential storage.');
    }
  }

  async list() {
    return (await readJson(this.filePath, [])) || [];
  }

  async getMetadata(credentialRef) {
    const items = await this.list();
    const record = items.find((item) => item.credentialRef === credentialRef);
    if (!record) return null;
    return {
      credentialRef: record.credentialRef,
      kind: record.kind,
      createdAt: record.createdAt,
      updatedAt: record.updatedAt,
      tokenType: record.tokenType,
      userType: record.userType,
      companyId: record.companyId || null,
      locationId: record.locationId || null,
      scopes: record.scopes || []
    };
  }

  async getSecret(credentialRef) {
    this.assertReady();
    const items = await this.list();
    const record = items.find((item) => item.credentialRef === credentialRef);
    if (!record) return null;
    return {
      ...record,
      secret: decryptJson(record.secret, this.secret)
    };
  }

  async upsert(credentialRef, payload) {
    this.assertReady();
    const items = await this.list();
    const now = new Date().toISOString();
    const existingIndex = items.findIndex((item) => item.credentialRef === credentialRef);
    const record = {
      credentialRef,
      kind: payload.kind,
      tokenType: payload.tokenType,
      userType: payload.userType || null,
      companyId: payload.companyId || null,
      locationId: payload.locationId || null,
      scopes: payload.scopes || [],
      createdAt: existingIndex >= 0 ? items[existingIndex].createdAt : now,
      updatedAt: now,
      secret: encryptJson(payload.secret, this.secret)
    };

    if (existingIndex >= 0) items[existingIndex] = record;
    else items.push(record);

    await writeJson(this.filePath, items);
    return await this.getMetadata(credentialRef);
  }
}
