/**
 * Diffs propostos pelo motor (conteúdo `diff` de tool calls ACP) no editor
 * nativo de diff do VS Code, com documentos virtuais para o antes e o depois.
 */

import * as path from 'node:path';
import * as vscode from 'vscode';
import type { Diff } from '../protocol/acpTypes';

export const PROPOSAL_SCHEME = 'dengcode-proposta';

interface Proposal {
  key: string;
  permissionId: string | undefined;
  toolCallId: string;
  diffs: Diff[];
}

/** Parte da query de uma URI de proposta. */
export function parseProposalUri(uri: vscode.Uri): { key: string; index: number; side: 'old' | 'new' } | undefined {
  if (uri.scheme !== PROPOSAL_SCHEME) {
    return undefined;
  }
  const params = new URLSearchParams(uri.query);
  const key = params.get('p');
  const index = Number(params.get('i'));
  const side = params.get('lado') === 'antes' ? 'old' : 'new';
  if (!key || Number.isNaN(index)) {
    return undefined;
  }
  return { key, index, side };
}

export class DiffManager implements vscode.TextDocumentContentProvider, vscode.Disposable {
  private readonly proposals = new Map<string, Proposal>();
  private readonly changeEmitter = new vscode.EventEmitter<vscode.Uri>();
  readonly onDidChange = this.changeEmitter.event;
  private readonly registration: vscode.Disposable;

  constructor() {
    this.registration = vscode.workspace.registerTextDocumentContentProvider(PROPOSAL_SCHEME, this);
  }

  dispose(): void {
    this.registration.dispose();
    this.changeEmitter.dispose();
  }

  provideTextDocumentContent(uri: vscode.Uri): string {
    const parsed = parseProposalUri(uri);
    const diff = parsed ? this.proposals.get(parsed.key)?.diffs[parsed.index] : undefined;
    if (!parsed || !diff) {
      return '';
    }
    return parsed.side === 'old' ? (diff.oldText ?? '') : diff.newText;
  }

  /** Permissão pendente associada a uma URI de proposta (para os botões do editor). */
  permissionFor(uri: vscode.Uri): string | undefined {
    const parsed = parseProposalUri(uri);
    return parsed ? this.proposals.get(parsed.key)?.permissionId : undefined;
  }

  hasDiffs(toolCallId: string): boolean {
    return [...this.proposals.values()].some((p) => p.toolCallId === toolCallId);
  }

  /** Guarda os diffs de uma tool call (para abrir depois pelo chat). */
  register(toolCallId: string, diffs: Diff[], permissionId?: string): string {
    const key = permissionId ?? `tc-${toolCallId}`;
    this.proposals.set(key, { key, permissionId, toolCallId, diffs });
    return key;
  }

  /** Abre um editor de diff por arquivo proposto. */
  async show(key: string, options: { preserveFocus?: boolean } = {}): Promise<void> {
    const proposal = this.proposals.get(key);
    if (!proposal) {
      return;
    }
    for (let i = 0; i < proposal.diffs.length; i++) {
      const diff = proposal.diffs[i] as Diff;
      const base = path.basename(diff.path) || diff.path;
      const encodedPath = `/${encodeURIComponent(base)}`;
      const query = (side: string): string => new URLSearchParams({ p: key, i: String(i), lado: side }).toString();
      const left = vscode.Uri.from({ scheme: PROPOSAL_SCHEME, path: encodedPath, query: query('antes') });
      const right = vscode.Uri.from({ scheme: PROPOSAL_SCHEME, path: encodedPath, query: query('depois') });
      const suffix = diff.oldText == null ? 'arquivo novo' : 'proposta do DengCode';
      await vscode.commands.executeCommand('vscode.diff', left, right, `${base} (${suffix})`, {
        preview: false,
        preserveFocus: options.preserveFocus ?? false,
      });
    }
  }

  async showForToolCall(toolCallId: string): Promise<void> {
    const proposal = [...this.proposals.values()].reverse().find((p) => p.toolCallId === toolCallId);
    if (proposal) {
      await this.show(proposal.key);
    }
  }

  /** Fecha as abas de diff da proposta (ao aceitar/rejeitar). */
  async close(key: string): Promise<void> {
    const tabs: vscode.Tab[] = [];
    for (const group of vscode.window.tabGroups.all) {
      for (const tab of group.tabs) {
        const input = tab.input;
        if (input instanceof vscode.TabInputTextDiff && parseProposalUri(input.modified)?.key === key) {
          tabs.push(tab);
        }
      }
    }
    if (tabs.length > 0) {
      await vscode.window.tabGroups.close(tabs, true);
    }
  }

  /** Abas de proposta abertas (usado nos testes). */
  openProposalTabs(): vscode.Tab[] {
    return vscode.window.tabGroups.all.flatMap((g) =>
      g.tabs.filter((t) => t.input instanceof vscode.TabInputTextDiff && t.input.modified.scheme === PROPOSAL_SCHEME),
    );
  }
}
