import { generateFacebookAdPreview } from './facebook-ad-preview.js';
import { generateGoogleAdPreview } from './google-ad-preview.js';
import { generateSocialPostPreview } from './social-post-preview.js';

export class PreviewArtifactsAdapter {
  async generateFacebookAdPreview(_credentialRef, { locationId, campaignId, adsetId, adId, campaign, adset, ad, sourcePost, outputDir } = {}) {
    return generateFacebookAdPreview({ locationId, campaignId, adsetId, adId, campaign, adset, ad, sourcePost, outputDir });
  }

  async generateGoogleAdPreview(credentialRef, { locationId, adId, outputDir } = {}) {
    return generateGoogleAdPreview({ credentialRef, locationId, adId, outputDir });
  }

  async generateSocialPostPreview(_credentialRef, { locationId, postId, post, socialAccounts, outputDir } = {}) {
    return generateSocialPostPreview({ locationId, postId, post, socialAccounts, outputDir });
  }
}
