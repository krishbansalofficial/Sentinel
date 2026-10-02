import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Field, FormDialog, useDialogState, useFormAction } from "@/components/FormDialog";
import { StatusLabel } from "@/components/product";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { http } from "@/lib/api";
import type { ProviderConnectionStatus } from "@/lib/api/types";
import { connectGithub, connectGitlab, disconnectGithub, disconnectGitlab } from "@/services/actions";

const PROVIDER = [["providers"]] as const;

const PROVIDERS = {
  github: {
    name: "GitHub",
    connect: connectGithub,
    disconnect: disconnectGithub,
    tokenHint: "Needs repository read and pull-request write for the repositories you use.",
    disconnectEffect: "Existing grants stop working, and pull-request actions are unavailable until you connect again.",
  },
  gitlab: {
    name: "GitLab",
    connect: connectGitlab,
    disconnect: disconnectGitlab,
    tokenHint: "Needs read_api for the gitlab.com projects whose CI results you want to read.",
    disconnectEffect: "Existing GitLab grants stop working, and CI results for gitlab.com repositories can't be refreshed until you connect again.",
  },
} as const;

/** A provider's connection state and its two actions. The token is sent once and never displayed or kept. */
export function ProviderConnection({ provider }: { provider: keyof typeof PROVIDERS }) {
  const p = PROVIDERS[provider];
  // Same key as githubStatusQuery/gitlabStatusQuery, so every screen shares one cached status.
  const status = useQuery({ queryKey: ["providers", provider, "status"] as const, queryFn: ({ signal }) => http.get<ProviderConnectionStatus>(`/api/v1/providers/${provider}/status`, { signal }) });
  const [token, setToken] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const dlg = useDialogState(() => { setToken(""); setErr(null); connect.reset(); connect.renewKey(); });
  const connect = useFormAction({ run: (t: string) => p.connect(t), invalidate: [...PROVIDER], onSuccess: () => { setToken(""); dlg.close(); } });
  const disconnect = useFormAction({ run: () => p.disconnect(), invalidate: [...PROVIDER], onSuccess: () => setConfirm(false) });
  const configured = status.data?.configured;
  const field = `${provider}-token`;

  return (
    <div className="flex flex-wrap items-center gap-3">
      {status.isPending ? (
        <span className="text-sm text-muted-foreground" role="status">Checking…</span>
      ) : status.isError ? (
        <StatusLabel status={{ label: "Status unavailable", tone: "warn" }} />
      ) : (
        <StatusLabel status={configured ? { label: "Connected", tone: "ok" } : { label: "Not connected", tone: "neutral" }} />
      )}
      <Button size="sm" variant={configured ? "outline" : "default"} onClick={dlg.show}>{configured ? "Replace token" : `Connect ${p.name}`}</Button>
      {configured ? <Button size="sm" variant="ghost" className="text-danger" onClick={() => { disconnect.reset(); setConfirm(true); }}>Disconnect</Button> : null}

      <FormDialog
        open={dlg.open}
        onOpenChange={dlg.onOpenChange}
        title={`Connect ${p.name}`}
        description="Paste a personal access token. It is stored in this computer's credential store, sent once, and never shown again."
        submitLabel="Connect"
        pending={connect.isPending}
        error={connect.error}
        submit={() => (token.trim() ? (setErr(null), connect.mutate(token.trim())) : setErr("Paste a token to continue."))}
      >
        <Field id={field} label="Personal access token" error={err} hint={p.tokenHint}>
          <Input id={field} type="password" autoComplete="off" spellCheck={false} value={token} onChange={(e) => setToken(e.target.value)} aria-describedby={`${field}-h`} />
        </Field>
      </FormDialog>

      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title={`Disconnect ${p.name}?`}
        description={`The stored token is removed. ${p.disconnectEffect}`}
        confirmLabel="Disconnect"
        pending={disconnect.isPending}
        error={disconnect.error}
        onConfirm={() => disconnect.mutate(undefined)}
      />
    </div>
  );
}

/** GitHub connection state and its two actions. Shared by Settings and Delivery. */
export function GithubConnection() {
  return <ProviderConnection provider="github" />;
}

/** GitLab connection: CI results for repositories whose origin is on gitlab.com. */
export function GitlabConnection() {
  return <ProviderConnection provider="gitlab" />;
}
