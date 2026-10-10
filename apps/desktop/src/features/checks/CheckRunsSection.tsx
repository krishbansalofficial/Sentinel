import { useQuery } from "@tanstack/react-query";
import { ErrorState } from "@/components/ErrorState";
import { DataTable, EmptyState, Section, Skeleton, StatusLabel, td, th } from "@/components/product";
import type { CheckRunView } from "@/lib/api/types";
import { formatRelative } from "@/lib/status";
import { checkRunsQuery } from "@/services/changes";
import { checkBoundaryInfo, linuxFactsLines, networkText } from "./checks";

/** Every check run of a Change and the boundary it was observed to run under, with the facts behind it. */
export function CheckRunsSection({ changeId }: { changeId: string }) {
  const runs = useQuery(checkRunsQuery(changeId));
  if (runs.isPending) return <Section title="Check runs" flush><Skeleton lines={2} label="Loading check runs" /></Section>;
  if (runs.isError) return <ErrorState error={runs.error} onRetry={() => runs.refetch()} />;
  const items = runs.data.items ?? [];
  return (
    <Section
      title="Check runs"
      description="Recorded check runs and their boundary facts. Confined boundaries are shown only when the records verify them."
      flush
    >
      {!items.length ? (
        <EmptyState title="No check runs">Verification, assurance checks and diff coverage each record a run here.</EmptyState>
      ) : (
        <DataTable label="Check runs">
          <thead>
            <tr>
              <th className={th}>Run</th>
              <th className={th}>Boundary</th>
              <th className={th}>State</th>
              <th className={th}>Network</th>
              <th className={th}>Exit</th>
              <th className={th}>Facts</th>
            </tr>
          </thead>
          <tbody>{items.map((run) => <CheckRunRow key={run.id} run={run} />)}</tbody>
        </DataTable>
      )}
    </Section>
  );
}

function CheckRunRow({ run }: { run: CheckRunView }) {
  const boundary = checkBoundaryInfo(run.boundary);
  const facts = factsLines(run);
  return (
    <tr>
      <td className={`${td} mono`} title={run.id}>
        {run.id.slice(0, 8)}
        {run.created_at ? <div className="text-xs text-muted-foreground">{formatRelative(run.created_at)}</div> : null}
      </td>
      <td className={td}><span title={boundary.detail}><StatusLabel status={boundary} /></span></td>
      <td className={td}>{run.state}</td>
      <td className={td}>{networkText(run.network)}</td>
      <td className={`${td} mono`}>{run.timed_out ? "timed out" : run.exit_code ?? "—"}</td>
      <td className={td}>
        {facts.length ? (
          <ul className="space-y-0.5 text-xs text-muted-foreground" aria-label="Boundary facts">
            {facts.map((line) => <li key={line} className="break-words">{line}</li>)}
          </ul>
        ) : <span className="text-xs text-muted-foreground">—</span>}
      </td>
    </tr>
  );
}

function factsLines(run: CheckRunView): string[] {
  if (run.linux_sandbox) return linuxFactsLines(run.linux_sandbox);
  const token = run.token;
  if (!token) return [];
  return [
    token.is_appcontainer && token.job_verified ? "Token and Job Object verified" : "Token verification failed",
    `Integrity ${token.integrity_rid}`,
    `Package SID ${token.package_sid}`,
  ];
}
