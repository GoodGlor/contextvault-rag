import { api } from "./client";

// Mirrors the analytics schemas in src/contextvault/api/analytics.py.

export interface RepositoryVolume {
  repository_id: string;
  repository_name: string;
  query_count: number;
  not_in_vault_count: number;
}

export interface QuestionCount {
  question: string;
  ask_count: number;
}

export interface UserActivity {
  user_id: string;
  username: string;
  query_count: number;
}

export interface DailyVolume {
  day: string;
  total: number;
  not_in_vault: number;
}

export interface AnalyticsOverview {
  total_queries: number;
  answered: number;
  not_in_vault: number;
  not_in_vault_rate: number;
  per_repository: RepositoryVolume[];
  top_questions: QuestionCount[];
  active_users: UserActivity[];
  by_day: DailyVolume[];
}

/** The composite usage summary for the admin dashboard (admin-only). */
export function getAnalytics(topLimit = 10): Promise<AnalyticsOverview> {
  return api.get<AnalyticsOverview>(`/analytics?top_limit=${topLimit}`);
}
