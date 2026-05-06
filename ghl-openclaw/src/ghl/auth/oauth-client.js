import { DEFAULT_DOCS_VERSION_HEADER } from '../../config/constants.js';
import { getEnv } from '../../config/env.js';
import { httpRequest } from '../../lib/http.js';

function normalizeScopes(scopeValue) {
  if (!scopeValue) return [];
  if (Array.isArray(scopeValue)) return scopeValue;
  return String(scopeValue).split(/\s+/).map((item) => item.trim()).filter(Boolean);
}

export class GhlOAuthClient {
  constructor() {
    this.env = getEnv();
  }

  assertClientCredentials() {
    if (!this.env.clientId || !this.env.clientSecret) {
      throw new Error('GHL_CLIENT_ID and GHL_CLIENT_SECRET are required.');
    }
  }

  async exchangeAuthorizationCode({ code, userType, redirectUri }) {
    this.assertClientCredentials();
    const payload = new URLSearchParams({
      client_id: this.env.clientId,
      client_secret: this.env.clientSecret,
      grant_type: 'authorization_code',
      code,
      user_type: userType,
      ...(redirectUri || this.env.redirectUri ? { redirect_uri: redirectUri || this.env.redirectUri } : {})
    });

    return await httpRequest(`${this.env.apiBaseUrl}/oauth/token`, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body: payload.toString()
    });
  }

  async refreshAccessToken({ refreshToken, userType, redirectUri }) {
    this.assertClientCredentials();
    const payload = new URLSearchParams({
      client_id: this.env.clientId,
      client_secret: this.env.clientSecret,
      grant_type: 'refresh_token',
      refresh_token: refreshToken,
      user_type: userType,
      ...(redirectUri || this.env.redirectUri ? { redirect_uri: redirectUri || this.env.redirectUri } : {})
    });

    return await httpRequest(`${this.env.apiBaseUrl}/oauth/token`, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded'
      },
      body: payload.toString()
    });
  }

  async exchangeAgencyForLocationToken({ agencyAccessToken, companyId, locationId }) {
    const payload = new URLSearchParams({ companyId, locationId });
    return await httpRequest(`${this.env.apiBaseUrl}/oauth/locationToken`, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded',
        Version: DEFAULT_DOCS_VERSION_HEADER,
        Authorization: `Bearer ${agencyAccessToken}`
      },
      body: payload.toString()
    });
  }

  toStoredSecret(tokenResponse) {
    const data = tokenResponse.data || {};
    return {
      accessToken: data.access_token,
      refreshToken: data.refresh_token,
      tokenType: data.token_type || 'Bearer',
      expiresIn: data.expires_in || null,
      scopes: normalizeScopes(data.scope),
      userType: data.userType || data.user_type || null,
      companyId: data.companyId || null,
      locationId: data.locationId || null,
      userId: data.userId || null,
      refreshTokenId: data.refreshTokenId || null,
      traceId: data.traceId || null,
      receivedAt: new Date().toISOString()
    };
  }
}
