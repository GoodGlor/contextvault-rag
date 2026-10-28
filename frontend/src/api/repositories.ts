import { api } from "./client";

// Mirrors RepositoryResponse in src/contextvault/api/repositories.py.
export interface Repository {
  id: string;
  name: string;
  description: string | null;
}

/** The repositories the current user can actively reach (their granted picker). */
export function listRepositories(): Promise<Repository[]> {
  return api.get<Repository[]>("/repositories");
}

// --- admin repository management ---------------------------------

// Mirrors LLMProviderName in src/contextvault/models/enums.py.
export type LLMProvider = "gemini" | "openai" | "openrouter" | "anthropic" | "custom";

/** Display labels for the provider select, in a stable order. */
export const LLM_PROVIDERS: { value: LLMProvider; label: string }[] = [
  { value: "anthropic", label: "Anthropic" },
  { value: "openai", label: "OpenAI" },
  { value: "gemini", label: "Google (Gemini)" },
  { value: "openrouter", label: "OpenRouter" },
  { value: "custom", label: "Custom (local / self-hosted)" },
];

// Mirrors AdminRepositoryResponse in src/contextvault/api/repositories.py.
export interface AdminRepository {
  id: string;
  name: string;
  description: string | null;
  configured: boolean;
}

// Mirrors LLMConfigResponse in src/contextvault/api/repositories.py. A repo picks a
// provider + model; the API key is global (Providers settings), not stored here.
// `configured` means answerable: a model is picked and its provider has a verified key.
export interface LLMConfig {
  provider: LLMProvider | null;
  model: string | null;
  configured: boolean;
}

/** Every repository with its config state (admin-only). */
export function listAllRepositories(): Promise<AdminRepository[]> {
  return api.get<AdminRepository[]>("/admin/repositories");
}

/** Create a repository (admin-only); it starts unconfigured. */
export function createRepository(input: {
  name: string;
  description?: string | null;
}): Promise<AdminRepository> {
  return api.post<AdminRepository>("/repositories", input);
}

/** Update a repository's name and/or description (admin-only). Omitted fields are
 *  left unchanged; `description: null` clears it. */
export function updateRepository(
  id: string,
  input: { name?: string; description?: string | null },
): Promise<AdminRepository> {
  return api.patch<AdminRepository>(`/repositories/${id}`, input);
}

/** Delete a repository (admin-only); the caller must echo its name. Its sources,
 *  chunks, and grants cascade away with it. */
export function deleteRepository(id: string, confirmName: string): Promise<void> {
  return api.del<void>(`/repositories/${id}`, { confirm_name: confirmName });
}

/** Read a repository's LLM config (key masked; nulls if unconfigured). */
export function getLlmConfig(repositoryId: string): Promise<LLMConfig> {
  return api.get<LLMConfig>(`/repositories/${repositoryId}/llm-config`);
}

/** Pick the provider + model a repository answers with. The API key is not sent — it
 *  is shared from the global provider settings; the provider must already be verified. */
export function setLlmConfig(
  repositoryId: string,
  input: { provider: LLMProvider; model: string },
): Promise<LLMConfig> {
  return api.put<LLMConfig>(`/repositories/${repositoryId}/llm-config`, input);
}

// Mirrors ListModelsResponse in src/contextvault/api/repositories.py.
export interface ModelList {
  models: string[];
}

/** Fetch a provider's available models for the model dropdown (admin-only), using that
 *  provider's global (verified) key. The provider must have a key in Providers settings. */
export function listModels(
  repositoryId: string,
  input: { provider: LLMProvider },
): Promise<ModelList> {
  return api.post<ModelList>(`/repositories/${repositoryId}/llm-models`, input);
}

/** The repository's default template for reports generated from now on. Null
 *  means no choice has been made and new reports get the shipped default.
 *  Existing reports are untouched by a change here — each one snapshotted its
 *  template at generation. */
export function getReportTemplate(repositoryId: string): Promise<{ template: string | null }> {
  return api.get<{ template: string | null }>(`/repositories/${repositoryId}/report-template`);
}

export function setReportTemplate(
  repositoryId: string,
  template: string,
): Promise<{ template: string | null }> {
  return api.put<{ template: string | null }>(`/repositories/${repositoryId}/report-template`, {
    template,
  });
}
