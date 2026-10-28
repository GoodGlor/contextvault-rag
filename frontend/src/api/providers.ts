import { api } from "./client";
import type { LLMProvider } from "./repositories";

// Mirrors ProviderStatusResponse in src/contextvault/api/providers.py. The key is
// write-only: it comes back only masked, never in full after entry.
export interface ProviderStatus {
  provider: LLMProvider;
  configured: boolean;
  verified: boolean;
  api_key_masked: string | null;
  base_url: string | null;
}

/** Every provider with its key status (admin-only). Custom endpoints appear here too. */
export function listProviders(): Promise<ProviderStatus[]> {
  return api.get<ProviderStatus[]>("/admin/providers");
}

/** Store (and first verify) a provider's config. Cloud providers need `apiKey`; a
 *  custom OpenAI-compatible endpoint needs `baseUrl` and may omit the key. */
export function setProviderKey(
  provider: LLMProvider,
  input: { apiKey?: string; baseUrl?: string },
): Promise<ProviderStatus> {
  return api.put<ProviderStatus>(`/admin/providers/${provider}`, {
    api_key: input.apiKey ?? null,
    base_url: input.baseUrl ?? null,
  });
}

/** Remove a provider's stored key (admin-only). */
export function deleteProviderKey(provider: LLMProvider): Promise<void> {
  return api.del<void>(`/admin/providers/${provider}`);
}
