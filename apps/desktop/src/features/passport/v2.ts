import type { StatusInfo } from "../../lib/status.ts";

/** Every ExecutionBoundary value Passport v2 can claim, worded for a person. */
export const BOUNDARIES: Record<string, StatusInfo & { detail: string }> = {
  APPCONTAINER: { label: "AppContainer verified", tone: "ok", detail: "Every launch ran in a verified AppContainer (token, Low integrity, Job Object checked before it resumed)." },
  LINUX_SANDBOX: { label: "Linux sandbox verified", tone: "ok", detail: "Every launch ran in a verified Linux sandbox (separate namespaces, a seccomp filter and its own cgroup, checked before it ran)." },
  RESTRICTED_TOKEN: { label: "Restricted token", tone: "warn", detail: "A launch ran with a reduced token, which does not isolate files or network." },
  UNCONFINED: { label: "Unconfined", tone: "danger", detail: "A launch ran with your full account authority." },
  UNKNOWN: { label: "Unknown", tone: "neutral", detail: "The records cannot establish a boundary (an attached run, missing or inconsistent facts, or no launch)." },
};

export function boundaryClaimInfo(boundary: string | null | undefined): StatusInfo & { detail: string } {
  return BOUNDARIES[boundary ?? ""] ?? { label: boundary ? `Unrecognized (${boundary})` : "Unknown", tone: "neutral", detail: "This backend reported a boundary this app does not recognize." };
}

/** Repository-contract load failures (backend/app/policy/repo_contract.py), worded for a person. */
export const CONTRACT_LOAD_ERRORS: Record<string, string> = {
  REPOSITORY_CONTRACT_MISSING: "The baseline commit has no .sentinel/contract.toml.",
  REPOSITORY_CONTRACT_INVALID: "The committed .sentinel/contract.toml is not a valid Change Contract.",
  REPOSITORY_CONTRACT_UNAVAILABLE: "The contract can't be read: capture a baseline first, or the baseline commit is not in the repository.",
};

export function contractLoadErrorText(code: string | null | undefined, fallback: string): string {
  return (code && CONTRACT_LOAD_ERRORS[code]) || fallback;
}
