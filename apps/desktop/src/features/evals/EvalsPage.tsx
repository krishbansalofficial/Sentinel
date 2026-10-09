import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { compareRuns } from "./compare";
import { http } from "@/lib/api";
import type { components } from "@/lib/api/generated/schema";
import { ErrorState } from "@/components/ErrorState";
import { DataTable, EmptyState, PageHeader, Section, Skeleton, td, th } from "@/components/product";
import { formatTime } from "@/lib/status";

type Runs = components["schemas"]["EvalRunListResponse"];
const percent = (value: number) => `${(value * 100).toFixed(1)}%`;

export function EvalsPage() {
  const runs = useQuery({
    queryKey: ["evals", "runs"],
    queryFn: ({ signal }) => http.get<Runs>("/api/v1/evals/runs", { signal }),
    refetchInterval: 10_000,
  });
  return <>
    <PageHeader title="Eval" description="Recorded agent evaluations, with pass rates and Wilson 95% intervals." />
    {runs.isPending ? <Section><Skeleton lines={4} label="Loading evaluations" /></Section>
      : runs.isError ? <ErrorState error={runs.error} onRetry={() => runs.refetch()} />
      : !runs.data.items.length ? <Section><EmptyState title="No evaluations recorded">
        Run sentinel eval with --record to save results here.
      </EmptyState></Section>
      : <>{runs.data.items.length >= 2 && <ComparePanel runs={runs.data.items} />}
      {runs.data.items.map(run => <Section key={run.id}>
        <h2 className="font-semibold">{run.config_name} · {run.agent}{run.model ? ` · ${run.model}` : ""}</h2>
        <p className="text-sm text-muted-foreground">{run.suite} · {formatTime(run.started_at)} · {run.completed_at ? "Completed" : "In progress"}</p>
        <p className="text-sm">{run.passes}/{run.attempts} passed · {percent(run.rate)} ({percent(run.low)}–{percent(run.high)}) · {run.errors} errors</p>
        <p className="text-sm text-muted-foreground">Mean cost: {run.mean_cost_usd == null ? "Unknown" : `$${run.mean_cost_usd.toFixed(4)}`} · {run.cost_unknown} attempts with unknown cost · p50/p95 time: {run.wall_p50_seconds?.toFixed(2) ?? "Unknown"}/{run.wall_p95_seconds?.toFixed(2) ?? "Unknown"} s</p>
        <p className="text-sm text-muted-foreground">Hidden-test boundaries: {Object.entries(run.hidden_boundaries ?? {}).map(([name, count]) => `${name}: ${count}`).join(", ") || "Unknown"}</p>
        <details><summary className="cursor-pointer text-sm">Task results</summary>
          <DataTable label={`Tasks for ${run.config_name}`}><thead><tr>
            {["Task", "Passed", "Errors", "Pass rate (95% interval)"].map(label => <th key={label} className={th}>{label}</th>)}
          </tr></thead><tbody>{(run.tasks ?? []).map(task => <tr key={task.task_id}>
            <td className={td}>{task.task_id}</td><td className={td}>{task.passes}/{task.attempts}</td>
            <td className={td}>{task.errors}</td><td className={td}>{percent(task.rate)} ({percent(task.low)}–{percent(task.high)})</td>
          </tr>)}</tbody></DataTable>
        </details>
      </Section>)}</>}
  </>;
}

type Run = Runs["items"][number];
const signed = (value: number) => `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)} pts`;

function ComparePanel({ runs }: { runs: Run[] }) {
  // Newest first from the API: default to "previous run" vs "latest run".
  const [baselineId, setBaselineId] = useState(runs[1]?.id ?? "");
  const [candidateId, setCandidateId] = useState(runs[0]?.id ?? "");
  const baseline = runs.find(run => run.id === baselineId);
  const candidate = runs.find(run => run.id === candidateId);
  const result = baseline && candidate && baseline.id !== candidate.id ? compareRuns(baseline, candidate) : null;
  const picker = (label: string, value: string, onChange: (id: string) => void) => (
    <label className="flex flex-col gap-1 text-sm">{label}
      <select aria-label={label} className="rounded border bg-background px-2 py-1" value={value}
        onChange={event => onChange(event.target.value)}>
        {runs.map(run => <option key={run.id} value={run.id}>
          {run.config_name} · {formatTime(run.started_at)} · {percent(run.rate)}</option>)}
      </select></label>);
  return <Section>
    <h2 className="font-semibold">Compare runs</h2>
    <div className="flex flex-wrap gap-4">
      {picker("Baseline (A)", baselineId, setBaselineId)}
      {picker("Candidate (B)", candidateId, setCandidateId)}
    </div>
    {!result ? <p className="text-sm text-muted-foreground">Pick two different runs.</p> : <>
      <p className="text-sm" role="status">
        Overall {signed(result.overallDelta)} ·{" "}
        {result.intervalsSeparateDown ? <strong className="text-red-600">Regression: the 95% intervals do not overlap</strong>
          : result.intervalsSeparateUp ? <strong className="text-green-700">Improvement: the 95% intervals do not overlap</strong>
          : "Intervals overlap: not a regression by the interval test"}
      </p>
      <p className="text-xs text-muted-foreground">
        The paired bootstrap needs per-attempt results; run <code>sentinel eval compare A.json B.json</code> for it.
      </p>
      <DataTable label="Per-task changes"><thead><tr>
        {["Task", "A", "B", "Change", ""].map(label => <th key={label} className={th}>{label}</th>)}
      </tr></thead><tbody>{result.tasks.map(task => <tr key={task.task_id}>
        <td className={td}>{task.task_id}</td><td className={td}>{percent(task.rateA)}</td>
        <td className={td}>{percent(task.rateB)}</td><td className={td}>{signed(task.delta)}</td>
        <td className={td}>{task.flip === "regressed" ? "Regressed" : task.flip === "improved" ? "Improved" : ""}</td>
      </tr>)}</tbody></DataTable>
      {(result.onlyInA.length > 0 || result.onlyInB.length > 0) && <p className="text-xs text-muted-foreground">
        Only in A: {result.onlyInA.join(", ") || "none"} · Only in B: {result.onlyInB.join(", ") || "none"}</p>}
    </>}
  </Section>;
}
