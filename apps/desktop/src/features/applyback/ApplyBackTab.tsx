import { useQuery } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";
import { useState } from "react";
import { ErrorState } from "@/components/ErrorState";
import { Field, FormDialog, useDialogState, useFormAction } from "@/components/FormDialog";
import { ActorPicker } from "@/components/pickers";
import { DataTable, EmptyState, Facts, Notice, Section, Skeleton, StatusLabel, td, th } from "@/components/product";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useActors } from "@/features/authority/useActors";
import { ApiError } from "@/lib/api/client";
import type { ChangeWorkspace, WorkspaceApplyPreview } from "@/lib/api/types";
import { formatRelative, formatTime, shortSha } from "@/lib/status";
import { cn } from "@/lib/utils";
import { changeKeys } from "@/services/changes";
import { applyWorkspace, discardWorkspace, presetQuery, previewWorkspace, workspaceQuery } from "@/services/workspace";
import { boundaryInfo, canApply, confirmationMatches, flaggedPaths, presetInfo, refusalText } from "./applyback";

type Actors = ReturnType<typeof useActors>["actors"];

/** Apply-back of the AppContainer workspace: show it, preview it, and apply or discard it. */
export function ApplyBackTab() {
  const changeId = useParams({ from: "/changes/$changeId" }).changeId;
  const workspace = useQuery(workspaceQuery(changeId));
  const preset = useQuery(presetQuery(changeId));
  const { actors } = useActors(changeId);
  const [preview, setPreview] = useState<WorkspaceApplyPreview | null>(null);
  const runPreview = useFormAction({
    run: () => previewWorkspace(changeId),
    invalidate: [[...changeKeys.all]],
    onSuccess: (result: WorkspaceApplyPreview) => setPreview(result),
  });

  if (workspace.isPending) return <Section flush><Skeleton lines={3} label="Loading workspace" /></Section>;
  const missing = workspace.isError && workspace.error instanceof ApiError && workspace.error.kind === "not_found";
  if (workspace.isError && !missing) return <ErrorState error={workspace.error} onRetry={() => workspace.refetch()} />;
  const ws = workspace.data;

  return (
    <>
      <PresetDecision decision={preset.data?.decision} name={preset.data?.preset_name} version={preset.data?.preset_version} denials={preset.data?.denials} failed={preset.isError} />
      {!ws ? (
        <Section><EmptyState title="No workspace">An AppContainer agent run works in a Sentinel-owned clone of your repository. Its changes reach your branch only through a previewed, approved fast-forward from here.</EmptyState></Section>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-2" role="toolbar" aria-label="Apply-back actions">
            <Button size="sm" variant={preview ? "outline" : "default"} disabled={runPreview.isPending || ws.state === "APPLIED" || ws.state === "CLEANED"} onClick={() => runPreview.mutate(undefined)}>
              {runPreview.isPending ? "Previewing…" : preview ? "Preview again" : "Preview apply-back"}
            </Button>
            {preview && canApply(preview) ? <ApplyDialog changeId={changeId} preview={preview} actors={actors} onDone={() => setPreview(null)} /> : null}
            {ws.state !== "APPLIED" && ws.state !== "CLEANED" ? <DiscardDialog changeId={changeId} actors={actors} onDone={() => setPreview(null)} /> : null}
          </div>
          {runPreview.isError ? <Notice tone="danger" title="Preview failed" role="alert">{runPreview.error instanceof ApiError ? runPreview.error.message : "The request failed."}</Notice> : null}
          {preview ? <PreviewView preview={preview} /> : null}
          <WorkspaceView workspace={ws} />
        </>
      )}
      <p className="text-xs leading-5 text-muted-foreground">Apply-back only fast-forwards your branch to the previewed commit. It never forces, resets or merges, and every new run or preview voids an earlier approval.</p>
    </>
  );
}

function PresetDecision({ decision, name, version, denials, failed }: { decision?: string; name?: string | null; version?: string | null; denials?: string[]; failed: boolean }) {
  if (failed) return <Notice tone="warn" title="Policy preset decision unavailable">The preset decision could not be read. Review-ready and pull-request steps stay closed while a selected preset can't decide ALLOW.</Notice>;
  const info = presetInfo(decision, name);
  return (
    <Section title="Policy preset" action={<StatusLabel status={info} />}>
      <Facts items={[
        { label: "Preset", value: name ? `${name} ${version ?? ""}`.trim() : "None selected (not gated)" },
        { label: "Unmet requirements", value: denials?.length ? <ul className="list-disc pl-5">{denials.map((d) => <li key={d} className="break-words">{d}</li>)}</ul> : "None" },
      ]} />
    </Section>
  );
}

function PreviewView({ preview }: { preview: WorkspaceApplyPreview }) {
  const refusal = refusalText(preview.refusal_reason);
  const forbidden = flaggedPaths(preview, "forbidden");
  const credential = flaggedPaths(preview, "credential");
  return (
    <>
      {refusal ? <Notice tone="danger" title="Apply-back is refused" role="alert">{refusal}</Notice> : <Notice title="Ready to apply" role="status">This preview can be applied to <code>{preview.user_branch ?? "your branch"}</code>.</Notice>}
      {forbidden.length ? <Notice tone="danger" title="Forbidden paths in the changes"><ul className="list-disc pl-5">{forbidden.map((p) => <li key={p} className="break-all">{p}</li>)}</ul></Notice> : null}
      {credential.length ? <Notice tone="danger" title="The staged credential appears in"><ul className="list-disc pl-5">{credential.map((p) => <li key={p} className="break-all">{p}</li>)}</ul></Notice> : null}
      <Section title="Changed paths" flush>
        {(preview.changed_paths ?? []).length === 0 ? <p className="px-5 py-4 text-sm text-muted-foreground">No paths changed.</p> : (
          <DataTable label="Changed paths">
            <thead><tr><th className={th}>Status</th><th className={th}>Path</th><th className={th}>Flags</th></tr></thead>
            <tbody>
              {(preview.changed_paths ?? []).map((item) => (
                <tr key={`${item.status}:${item.path}`}>
                  <td className={cn(td, "mono")}>{item.status}</td>
                  <td className={cn(td, "break-all mono")}>{item.path}</td>
                  <td className={td}>{item.flags?.length ? item.flags.join(", ") : "—"}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
      </Section>
      {preview.limitations?.length ? <Notice tone="warn" title="Limitations"><ul className="list-disc pl-5">{preview.limitations.map((l) => <li key={l} className="break-words">{l}</li>)}</ul></Notice> : null}
    </>
  );
}

function WorkspaceView({ workspace }: { workspace: ChangeWorkspace }) {
  return (
    <>
      <Section title="Workspace" description={`Updated ${formatRelative(workspace.updated_at)}`}>
        <Facts items={[
          { label: "State", value: workspace.state },
          { label: "AppContainer profile", value: <code className="break-all">{workspace.profile_name}</code> },
          { label: "Base", value: <code>{shortSha(workspace.base_sha)}</code> },
          { label: "Sealed", value: workspace.sealed_sha ? <code>{shortSha(workspace.sealed_sha)}</code> : "Not sealed" },
          ...(workspace.applied_sha ? [{ label: "Applied", value: <code>{shortSha(workspace.applied_sha)}</code> }] : []),
          ...(workspace.refusal_reason ? [{ label: "Last refusal", value: refusalText(workspace.refusal_reason) }] : []),
          { label: "Credential staged", value: workspace.credential_staged ? "Yes" : "No" },
        ]} />
      </Section>
      <Section title="Runs" flush>
        {(workspace.runs ?? []).length === 0 ? <p className="px-5 py-4 text-sm text-muted-foreground">No runs recorded.</p> : (
          <DataTable label="Workspace runs">
            <thead><tr><th className={th}>Run</th><th className={th}>Status</th><th className={th}>Boundary</th><th className={th}>Finished</th></tr></thead>
            <tbody>
              {(workspace.runs ?? []).map((run) => (
                <tr key={run.run_id}>
                  <td className={cn(td, "mono")} title={run.run_id}>{run.run_id.slice(0, 8)}</td>
                  <td className={td}>{run.status}</td>
                  <td className={td}><StatusLabel status={boundaryInfo(run.boundary)} /></td>
                  <td className={td} title={run.finished_at ? formatTime(run.finished_at) : undefined}>{run.finished_at ? formatRelative(run.finished_at) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
      </Section>
    </>
  );
}

function ApplyDialog({ changeId, preview, actors, onDone }: { changeId: string; preview: WorkspaceApplyPreview & { approval_token: string }; actors: Actors; onDone: () => void }) {
  const [actor, setActor] = useState("");
  const [typed, setTyped] = useState("");
  const [errs, setErrs] = useState<Record<string, string>>({});
  const branch = preview.user_branch ?? null;
  const dlg = useDialogState(() => { setActor(""); setTyped(""); setErrs({}); run.reset(); });
  const run = useFormAction({
    run: (body: { actor_id: string; approval_token: string }) => applyWorkspace(changeId, body),
    invalidate: [[...changeKeys.all]],
    onSuccess: () => { dlg.close(); onDone(); },
  });
  return (
    <>
      <Button size="sm" variant="destructive" onClick={dlg.show}>Apply to branch…</Button>
      <FormDialog
        open={dlg.open}
        onOpenChange={dlg.onOpenChange}
        title={`Fast-forward ${branch ?? "your branch"}?`}
        description="Your branch moves to the previewed commit. Nothing is forced, reset or merged; a moved branch refuses instead."
        submitLabel="Apply"
        danger
        pending={run.isPending}
        error={run.error}
        submit={() => {
          const e: Record<string, string> = {};
          if (!actor) e.actor = "Choose who holds the workspace.apply delegation.";
          if (!confirmationMatches(typed, branch)) e.typed = `Type ${branch ?? "the branch name"} to confirm.`;
          setErrs(e);
          if (Object.keys(e).length) return;
          run.mutate({ actor_id: actor, approval_token: preview.approval_token });
        }}
      >
        <ul className="list-disc space-y-1 pl-5 text-sm">{(preview.changed_paths ?? []).map((p) => <li key={p.path} className="break-all">{p.path}</li>)}</ul>
        <ActorPicker id="apply-actor" label="Applied by" actors={actors} value={actor} onChange={setActor} error={errs.actor} />
        <Field id="apply-typed" label={`Type ${branch ?? "the branch name"} to confirm`} error={errs.typed} hint="This ties your approval to the branch apply-back will move.">
          <Input id="apply-typed" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" spellCheck={false} className="mono" />
        </Field>
      </FormDialog>
    </>
  );
}

function DiscardDialog({ changeId, actors, onDone }: { changeId: string; actors: Actors; onDone: () => void }) {
  const [actor, setActor] = useState("");
  const [errs, setErrs] = useState<Record<string, string>>({});
  const dlg = useDialogState(() => { setActor(""); setErrs({}); run.reset(); });
  const run = useFormAction({
    run: (body: { actor_id: string }) => discardWorkspace(changeId, body),
    invalidate: [[...changeKeys.all]],
    onSuccess: () => { dlg.close(); onDone(); },
  });
  return (
    <>
      <Button size="sm" variant="outline" onClick={dlg.show}>Discard…</Button>
      <FormDialog
        open={dlg.open}
        onOpenChange={dlg.onOpenChange}
        title="Discard the workspace?"
        description="The unapplied workspace and its AppContainer profile are removed. Your repository is not touched."
        submitLabel="Discard"
        danger
        pending={run.isPending}
        error={run.error}
        submit={() => {
          if (!actor) { setErrs({ actor: "Choose who holds the workspace.discard delegation." }); return; }
          run.mutate({ actor_id: actor });
        }}
      >
        <ActorPicker id="discard-actor" label="Discarded by" actors={actors} value={actor} onChange={setActor} error={errs.actor} />
      </FormDialog>
    </>
  );
}
