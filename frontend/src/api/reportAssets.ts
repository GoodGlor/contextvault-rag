import { api } from "./client";

// Mirrors ReportAssetResponse in src/contextvault/api/report_assets.py.
//
// Assets are the first user-uploaded bytes this app persists: an uploaded
// *source* file is OCR'd and its bytes discarded, and every other binary column
// holds a generated artifact. They belong to a repository, not to a report, so
// the same logo can be worn by many reports and deleting a report never takes
// one away.

/** One uploaded image. Deliberately never its bytes — the list endpoint does not
 *  fetch them, so a library of logos costs one small response. */
export interface ReportAsset {
  id: string;
  filename: string;
  mime: string;
  width: number;
  height: number;
  byte_size: number;
}

/** What the upload accepts. Raster only, and that is a rendering constraint
 *  rather than taste: python-pptx places pictures and supports no SVG, so a
 *  vector logo would render in the PDF and leave a gap in the deck. */
export const ACCEPTED_IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp"] as const;

/** Rejected above this, server-side. Mirrored here only so the UI can say so
 *  before spending the upload — the server remains the decision. */
export const MAX_ASSET_BYTES = 2 * 1024 * 1024;

export function listReportAssets(repositoryId: string): Promise<ReportAsset[]> {
  return api.get<ReportAsset[]>(`/repositories/${repositoryId}/report-assets`);
}

/** The URL the renderers and the editor read an asset's bytes from. Access is
 *  checked server-side per repository; this is not a public link. */
export function reportAssetUrl(repositoryId: string, assetId: string): string {
  return `/repositories/${repositoryId}/report-assets/${assetId}`;
}

export function uploadReportAsset(repositoryId: string, file: File): Promise<ReportAsset> {
  const form = new FormData();
  form.append("file", file);
  return api.upload<ReportAsset>(`/repositories/${repositoryId}/report-assets`, form);
}

export function deleteReportAsset(repositoryId: string, assetId: string): Promise<void> {
  return api.del<void>(`/repositories/${repositoryId}/report-assets/${assetId}`);
}
