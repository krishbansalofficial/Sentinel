/** Simple argument grouping; no shell or command substitution is evaluated. */
export function splitArgs(raw: string): string[] {
  const out: string[] = [];
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  for (let m = re.exec(raw); m; m = re.exec(raw)) out.push(m[1] ?? m[2] ?? m[3] ?? "");
  return out;
}
