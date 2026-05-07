import { generateGoogleAdPreview } from './google-ad-preview.js';

export class PreviewArtifactsAdapter {
  async generateGoogleAdPreview(credentialRef, { locationId, adId, outputDir } = {}) {
    return generateGoogleAdPreview({ credentialRef, locationId, adId, outputDir });
  }
}
