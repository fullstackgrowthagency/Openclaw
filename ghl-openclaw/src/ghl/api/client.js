import { DEFAULT_DOCS_VERSION_HEADER } from '../../config/constants.js';
import { getEnv } from '../../config/env.js';
import { httpRequest } from '../../lib/http.js';
import { CredentialBroker } from '../auth/credential-broker.js';

export class GhlApiClient {
  constructor({ credentialBroker = new CredentialBroker() } = {}) {
    this.credentialBroker = credentialBroker;
    this.env = getEnv();
  }

  async request({ credentialRef, method = 'GET', path, query = {}, body = null, version = DEFAULT_DOCS_VERSION_HEADER }) {
    const token = await this.credentialBroker.getResolvedAccessToken(credentialRef);
    const url = new URL(`${this.env.apiBaseUrl}${path}`);
    for (const [key, value] of Object.entries(query || {})) {
      if (value === undefined || value === null || value === '') continue;
      url.searchParams.set(key, String(value));
    }

    const hasBody = body !== null && body !== undefined;
    const response = await httpRequest(url.toString(), {
      method,
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${token}`,
        Version: version,
        ...(hasBody ? { 'Content-Type': 'application/json' } : {})
      },
      body: hasBody ? JSON.stringify(body) : undefined
    });

    return response;
  }
}
