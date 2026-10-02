import { queryOptions } from "@tanstack/react-query";
import { http } from "@/lib/api";
import type { ChangeWorkspace, PolicyPresetEvaluation, WorkspaceApplyPreview, WorkspaceApplyResult } from "@/lib/api/types";
import { changeKeys } from "./changes";

const base = (id: string) => `/api/v1/changes/${encodeURIComponent(id)}`;

export const workspaceKeys = {
  workspace: (id: string) => [...changeKeys.all, "workspace", id] as const,
  preset: (id: string) => [...changeKeys.all, "policy-preset", id] as const,
};

/** The Change's Sentinel-owned workspace clone (404 when no AppContainer run created one). */
export const workspaceQuery = (id: string) =>
  queryOptions({ queryKey: workspaceKeys.workspace(id), queryFn: ({ signal }) => http.get<ChangeWorkspace>(`${base(id)}/workspace`, { signal }) });

/** The persisted policy preset decision, from the same snapshot a Passport v2 would sign. */
export const presetQuery = (id: string) =>
  queryOptions({ queryKey: workspaceKeys.preset(id), queryFn: ({ signal }) => http.get<PolicyPresetEvaluation>(`${base(id)}/policy/preset`, { signal }) });

/** Seals the workspace and describes apply-back; the user repository is only read. */
export const previewWorkspace = (id: string) => http.post<WorkspaceApplyPreview>(`${base(id)}/workspace/preview`, undefined);

/** Fast-forwards the user's branch to the previewed sealed commit; needs a workspace.apply delegation. */
export const applyWorkspace = (id: string, body: { actor_id: string; approval_token: string }) =>
  http.post<WorkspaceApplyResult>(`${base(id)}/workspace/apply`, body);

/** Removes the unapplied workspace and its profile; needs a workspace.discard delegation. */
export const discardWorkspace = (id: string, body: { actor_id: string }) => http.post<ChangeWorkspace>(`${base(id)}/workspace/discard`, body);
