export class CredentialBroker {
  constructor({ encryptedStore }) {
    this.encryptedStore = encryptedStore;
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
    return this.encryptedStore?.getMetadata
      ? this.encryptedStore.getMetadata(credentialRef)
      : { credentialRef, status: 'metadata_store_not_wired' };
  }

  async refreshOauthToken() {
    throw new Error('OAuth token refresh is not wired yet. Implement token exchange against live GHL credentials before production use.');
  }
}
