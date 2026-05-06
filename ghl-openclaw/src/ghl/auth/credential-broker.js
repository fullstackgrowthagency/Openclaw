import { FileEncryptedStore } from './encrypted-store.js';
import { GhlOAuthClient } from './oauth-client.js';

export class CredentialBroker {
  constructor({ encryptedStore = new FileEncryptedStore(), oauthClient = new GhlOAuthClient() } = {}) {
    this.encryptedStore = encryptedStore;
    this.oauthClient = oauthClient;
  }

  async issueGrant({ credentialRef, tokenType, locationId = null, scopeSet = [], approvalContext = null, expiresAt }) {
    return {
      credential_ref: credentialRef,
      token_type: tokenType,
      location_id: locationId,
      scope_set: scopeSet,
      approval_context: approvalContext,
      expires_at: expiresAt,
      raw_token: undefined
    };
  }

  async resolveCredentialMetadata(credentialRef) {
    return this.encryptedStore.getMetadata(credentialRef);
  }

  async storePit({ credentialRef, token, tokenType = 'private_integration_token', companyId = null, locationId = null, scopes = [] }) {
    return await this.encryptedStore.upsert(credentialRef, {
      kind: 'pit',
      tokenType,
      companyId,
      locationId,
      scopes,
      secret: {
        accessToken: token,
        tokenType: 'Bearer',
        scopes,
        companyId,
        locationId,
        receivedAt: new Date().toISOString()
      }
    });
  }

  async exchangeAuthorizationCode({ credentialRef, code, userType, redirectUri = null }) {
    const response = await this.oauthClient.exchangeAuthorizationCode({ code, userType, redirectUri });
    if (!response.ok) {
      throw new Error(`OAuth code exchange failed: ${response.status}`);
    }

    const secret = this.oauthClient.toStoredSecret(response);
    return await this.encryptedStore.upsert(credentialRef, {
      kind: 'oauth',
      tokenType: 'oauth_access_token',
      userType: secret.userType,
      companyId: secret.companyId,
      locationId: secret.locationId,
      scopes: secret.scopes,
      secret
    });
  }

  async refreshOauthToken(credentialRef) {
    const record = await this.encryptedStore.getSecret(credentialRef);
    if (!record) {
      throw new Error(`Credential not found: ${credentialRef}`);
    }
    if (!record.secret?.refreshToken) {
      throw new Error(`Credential has no refresh token: ${credentialRef}`);
    }

    const response = await this.oauthClient.refreshAccessToken({
      refreshToken: record.secret.refreshToken,
      userType: record.userType || record.secret.userType
    });

    if (!response.ok) {
      throw new Error(`OAuth refresh failed: ${response.status}`);
    }

    const secret = this.oauthClient.toStoredSecret(response);
    return await this.encryptedStore.upsert(credentialRef, {
      kind: 'oauth',
      tokenType: 'oauth_access_token',
      userType: secret.userType,
      companyId: secret.companyId,
      locationId: secret.locationId,
      scopes: secret.scopes,
      secret
    });
  }

  async exchangeAgencyForLocationToken({ agencyCredentialRef, targetCredentialRef, companyId, locationId }) {
    const record = await this.encryptedStore.getSecret(agencyCredentialRef);
    if (!record?.secret?.accessToken) {
      throw new Error(`Agency credential missing or invalid: ${agencyCredentialRef}`);
    }

    const response = await this.oauthClient.exchangeAgencyForLocationToken({
      agencyAccessToken: record.secret.accessToken,
      companyId,
      locationId
    });

    if (!response.ok) {
      throw new Error(`Agency-to-location token exchange failed: ${response.status}`);
    }

    const secret = this.oauthClient.toStoredSecret(response);
    return await this.encryptedStore.upsert(targetCredentialRef, {
      kind: 'oauth_location_exchange',
      tokenType: 'oauth_access_token',
      userType: secret.userType,
      companyId: secret.companyId,
      locationId: secret.locationId,
      scopes: secret.scopes,
      secret
    });
  }

  async getResolvedAccessToken(credentialRef) {
    const record = await this.encryptedStore.getSecret(credentialRef);
    if (!record?.secret?.accessToken) {
      throw new Error(`Credential missing access token: ${credentialRef}`);
    }
    return record.secret.accessToken;
  }
}
