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
