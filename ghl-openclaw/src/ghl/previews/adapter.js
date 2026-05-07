import { generateGoogleAdPreview } from './google-ad-preview.js';
import { generateSocialPostPreview } from './social-post-preview.js';

export class PreviewArtifactsAdapter {
  async generateGoogleAdPreview(credentialRef, { locationId, adId, outputDir } = {}) {
    return generateGoogleAdPreview({ credentialRef, locationId, adId, outputDir });
  }

  async generateSocialPostPreview(_credentialRef, { locationId, postId, post, socialAccounts, outputDir } = {}) {
    return generateSocialPostPreview({ locationId, postId, post, socialAccounts, outputDir });
  }
}
