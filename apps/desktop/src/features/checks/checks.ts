import type { StatusInfo } from "../../lib/status.ts";

/** Facts a Linux check box verified on its live process (``LinuxSandboxCheckFacts``). */
export interface LinuxCheckFacts {
  verified: boolean;
  separate_namespaces?: string[];
  seccomp_mode?: string | null;
  seccomp_filters_added?: number | null;
  no_new_privs: boolean;
  cgroup?: string | null;
  network_isolated: boolean;
}

/** Every boundary a check run can be observed under, worded for a person. Each box kind keeps its own name. */
export const CHECK_BOUNDARIES: Record<string, StatusInfo & { detail: string }> = {
  APPCONTAINER: { label: "AppContainer box", tone: "ok", detail: "Ran in a disposable AppContainer box whose token and Job Object were verified before it ran." },
  LINUX_SANDBOX: { label: "Linux sandbox box", tone: "ok", detail: "Ran in a disposable Linux sandbox box (separate namespaces, a seccomp filter and its own cgroup) verified before it ran." },
  UNCONFINED: { label: "Unconfined", tone: "danger", detail: "Ran at your account authority through the delegated checks.unconfined opt-in." },
};

const NOT_VERIFIED: StatusInfo & { detail: string } = {
  label: "Not verified",
  tone: "warn",
  detail: "The records cannot establish a verified boundary for this run (missing, inconsistent or failed facts).",
};

export function checkBoundaryInfo(boundary: string | null | undefined): StatusInfo & { detail: string } {
  if (!boundary) return NOT_VERIFIED;
  return CHECK_BOUNDARIES[boundary] ?? { label: `Unrecognized (${boundary})`, tone: "neutral", detail: "This backend reported a boundary this app does not recognize." };
}

/** One short line per verified fact, in the order a reviewer reads them; failed facts say so. */
export function linuxFactsLines(facts: LinuxCheckFacts | null | undefined): string[] {
  if (!facts) return [];
  const namespaces = facts.separate_namespaces ?? [];
  const added = facts.seccomp_filters_added;
  return [
    facts.verified ? "Verified before the check ran" : "Verification failed",
    namespaces.length ? `Separate namespaces: ${namespaces.join(", ")}` : "No separate namespaces recorded",
    facts.seccomp_mode === "2" && typeof added === "number" && added > 0
      ? `Seccomp filter mode, ${added} filter${added === 1 ? "" : "s"} added`
      : "No seccomp filter recorded",
    facts.no_new_privs ? "no_new_privs set" : "no_new_privs not set",
    facts.network_isolated ? "Network isolated" : "Network shared",
    ...(facts.cgroup ? [`Cgroup ${facts.cgroup}`] : []),
  ];
}

/** A compact network cell: a box run without a recorded network flag is shown as unknown, never as "off". */
export function networkText(network: boolean | null | undefined): string {
  if (network === true) return "On";
  if (network === false) return "Off";
  return "Unknown";
}
