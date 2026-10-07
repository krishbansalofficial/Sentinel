/** Pure comparison of two recorded eval runs (the desktop half of `sentinel eval compare`). */

export interface RunLike {
  id: string;
  config_name: string;
  passes: number;
  attempts: number;
  rate: number;
  low: number;
  high: number;
  tasks?: { task_id: string; passes: number; attempts: number; rate: number }[] | null;
}

export interface TaskDelta {
  task_id: string;
  rateA: number;
  rateB: number;
  delta: number;
  flip: "regressed" | "improved" | null;
}

export interface Comparison {
  overallDelta: number;
  /** B's interval lies entirely below A's: a drop that is not noise by the interval test. */
  intervalsSeparateDown: boolean;
  intervalsSeparateUp: boolean;
  tasks: TaskDelta[];
  onlyInA: string[];
  onlyInB: string[];
}

export function compareRuns(a: RunLike, b: RunLike): Comparison {
  const tasksA = new Map((a.tasks ?? []).map(task => [task.task_id, task.rate]));
  const tasksB = new Map((b.tasks ?? []).map(task => [task.task_id, task.rate]));
  const shared = [...tasksA.keys()].filter(id => tasksB.has(id)).sort();
  const tasks = shared.map(task_id => {
    const rateA = tasksA.get(task_id) ?? 0;
    const rateB = tasksB.get(task_id) ?? 0;
    const flip = rateA >= 0.5 && rateB < 0.5 ? "regressed" : rateB >= 0.5 && rateA < 0.5 ? "improved" : null;
    return { task_id, rateA, rateB, delta: rateB - rateA, flip } satisfies TaskDelta;
  });
  return {
    overallDelta: b.rate - a.rate,
    intervalsSeparateDown: b.high < a.low,
    intervalsSeparateUp: b.low > a.high,
    tasks,
    onlyInA: [...tasksA.keys()].filter(id => !tasksB.has(id)).sort(),
    onlyInB: [...tasksB.keys()].filter(id => !tasksA.has(id)).sort(),
  };
}

/** The backend's `sentinel eval compare` result: paired bootstrap plus the regression verdict. */
export interface ServerComparison {
  regression: boolean;
  overall_delta: number;
  bootstrap?: { mean_difference: number; low: number; high: number; resamples: number; tasks: number } | null;
}

export type VerdictTone = "regression" | "improvement" | "neutral";

const points = (value: number) => `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)} pts`;

/** One sentence for the verdict, naming the test that decided it. */
export function describeVerdict(server: ServerComparison): { tone: VerdictTone; text: string } {
  const boot = server.bootstrap;
  const interval = boot ? ` (paired bootstrap mean ${points(boot.mean_difference)}, 95% ${points(boot.low)} to ${points(boot.high)} over ${boot.tasks} tasks)` : "";
  if (server.regression) return { tone: "regression", text: `Regression${interval}` };
  if (boot && boot.low > 0) return { tone: "improvement", text: `Significant improvement${interval}` };
  return { tone: "neutral", text: `No significant regression${interval}` };
}
