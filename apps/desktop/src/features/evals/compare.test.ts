import assert from "node:assert/strict";
import test from "node:test";
import { compareRuns, type RunLike } from "./compare.ts";

const run = (id: string, rate: number, low: number, high: number,
  tasks: [string, number][]): RunLike => ({
  id, config_name: id, passes: 0, attempts: 0, rate, low, high,
  tasks: tasks.map(([task_id, taskRate]) => ({ task_id, passes: 0, attempts: 3, rate: taskRate })),
});

test("a separated drop is flagged and flipped tasks are listed", () => {
  const a = run("good", 0.95, 0.84, 0.99, [["x", 1], ["y", 1], ["z", 0]]);
  const b = run("broken", 0.1, 0.04, 0.24, [["x", 0], ["y", 1], ["w", 1]]);
  const result = compareRuns(a, b);
  assert.equal(result.intervalsSeparateDown, true);
  assert.equal(result.intervalsSeparateUp, false);
  assert.ok(Math.abs(result.overallDelta + 0.85) < 1e-9);
  assert.deepEqual(result.tasks.map(t => [t.task_id, t.flip]), [["x", "regressed"], ["y", null]]);
  assert.deepEqual(result.onlyInA, ["z"]);
  assert.deepEqual(result.onlyInB, ["w"]);
});

test("overlapping intervals are not flagged either way", () => {
  const a = run("a", 0.8, 0.6, 0.92, [["x", 1]]);
  const b = run("b", 0.7, 0.5, 0.85, [["x", 0.67]]);
  const result = compareRuns(a, b);
  assert.equal(result.intervalsSeparateDown, false);
  assert.equal(result.intervalsSeparateUp, false);
  assert.equal(result.tasks[0]?.flip, null);
});

test("an improvement is reported as such", () => {
  const result = compareRuns(run("a", 0.1, 0.02, 0.3, [["x", 0]]), run("b", 0.9, 0.7, 0.98, [["x", 1]]));
  assert.equal(result.intervalsSeparateUp, true);
  assert.equal(result.tasks[0]?.flip, "improved");
});

test("runs without task lists compare overall only", () => {
  const result = compareRuns({ ...run("a", 0.5, 0.3, 0.7, []), tasks: null }, run("b", 0.5, 0.3, 0.7, []));
  assert.deepEqual(result.tasks, []);
  assert.equal(result.overallDelta, 0);
});
