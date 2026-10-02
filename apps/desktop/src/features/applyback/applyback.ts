import type { StatusInfo } from "../../lib/status.ts";

/** Minimal shapes so these helpers stay pure and testable without the generated schema. */
export interface PreviewLike {
  approval_token?: string | null;
  refusal_reason?: string | null;
  fast_forward_possible?: boolean;
  changed_paths?: { path: string; flags?: string[] | null }[] | null;
}
export interface BoundaryLike {
  is_appcontainer?: boolean;
  job_verified?: boolean;
  integrity_rid?: string;
}

/** Every backend ApplyRefusal (backend/app/workspace/models.py), worded for a person. */
export const REFUSALS: Record<string, string> = {
  USER_BRANCH_MOVED: "Your branch moved since the workspace was created. Preview again after it settles.",
  USER_BRANCH_SWITCHED: "You switched branches since the workspace was created.",
  FAST_FORWARD_REFUSED: "Git refused the fast-forward; nothing was changed.",
  SEALED_COMMIT_MISMATCH: "The workspace changed after it was previewed. Preview again.",
  USER_HEAD_DETACHED: "Your repository has a detached HEAD; check out a branch first.",
  WORKSPACE_HISTORY_DIVERGED: "The workspace history diverged from your branch.",
  CREDENTIAL_IN_DIFF: "The changes contain the staged model credential. They can never be applied.",
  DIFF_TOO_LARGE_TO_SCAN: "The changes are too large to scan for the staged credential, so they are refused.",
  FORBIDDEN_PATH_IN_DIFF: "The changes touch a path the Change Contract forbids.",
};

export function refusalText(reason: string | null | undefined): string | null {
  if (!reason) return null;
  return REFUSALS[reason] ?? `Apply-back was refused (${reason}).`;
}

/** Apply is offered only for a refusal-free preview that carries an approval token. */
export function canApply(preview: PreviewLike | null | undefined): preview is PreviewLike & { approval_token: string } {
  return !!preview && !preview.refusal_reason && typeof preview.approval_token === "string" && preview.approval_token.length > 0 && preview.fast_forward_possible !== false;
}

export function flaggedPaths(preview: PreviewLike | null | undefined, flag: string): string[] {
  return (preview?.changed_paths ?? []).filter((item) => (item.flags ?? []).includes(flag)).map((item) => item.path);
}

/** A run's boundary is "verified" only when every recorded fact says so. */
export function boundaryInfo(boundary: BoundaryLike | null | undefined): StatusInfo {
  if (!boundary) return { label: "Not verified", tone: "warn" };
  if (boundary.is_appcontainer === true && boundary.job_verified === true && (boundary.integrity_rid ?? "").toLowerCase() === "0x1000") {
    return { label: "AppContainer verified", tone: "ok" };
  }
  return { label: "Verification failed", tone: "danger" };
}

export function presetInfo(decision: string | null | undefined, presetName: string | null | undefined): StatusInfo {
  if (!presetName) return { label: "No preset selected", tone: "neutral" };
  return decision === "ALLOW" ? { label: "Allowed", tone: "ok" } : { label: "Denied", tone: "danger" };
}

/** The confirmation is typed, not clicked: it must name the branch apply-back will move. */
export function confirmationMatches(typed: string, branch: string | null | undefined): boolean {
  return !!branch && typed.trim() === branch;
}
