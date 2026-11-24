/**
 * Pedidos de permissão pendentes (`session/request_permission`).
 *
 * Vários caminhos podem responder ao mesmo pedido (card no chat, botões do
 * diff, notificação, cancelamento): o primeiro vence e os outros viram no-op.
 * Puro (sem VS Code).
 */

import type { PermissionOption, RequestPermissionResponse } from '../protocol/acpTypes';

export type Decision = 'accept' | 'reject';

/** Mapeia Aceitar/Rejeitar para a opção do motor (prefere as de "uma vez"). */
export function pickOption(options: PermissionOption[], decision: Decision): PermissionOption | undefined {
  const order =
    decision === 'accept' ? (['allow_once', 'allow_always'] as const) : (['reject_once', 'reject_always'] as const);
  for (const kind of order) {
    const found = options.find((o) => o.kind === kind);
    if (found) {
      return found;
    }
  }
  return undefined;
}

export interface PendingPermission {
  permissionId: string;
  sessionId: string;
  toolCallId: string;
  options: PermissionOption[];
}

interface Entry extends PendingPermission {
  resolve: (response: RequestPermissionResponse) => void;
}

export class PermissionBroker {
  private seq = 0;
  private readonly pending = new Map<string, Entry>();
  private readonly resolvedListeners: Array<(permissionId: string, optionId: string | undefined) => void> = [];

  onResolved(listener: (permissionId: string, optionId: string | undefined) => void): void {
    this.resolvedListeners.push(listener);
  }

  /** Abre um pedido; a promessa resolve quando alguém decidir. */
  open(sessionId: string, toolCallId: string, options: PermissionOption[]): { permissionId: string; response: Promise<RequestPermissionResponse> } {
    this.seq++;
    const permissionId = `perm-${this.seq}`;
    const response = new Promise<RequestPermissionResponse>((resolve) => {
      this.pending.set(permissionId, { permissionId, sessionId, toolCallId, options, resolve });
    });
    return { permissionId, response };
  }

  get(permissionId: string): PendingPermission | undefined {
    return this.pending.get(permissionId);
  }

  list(): PendingPermission[] {
    return [...this.pending.values()];
  }

  findByToolCall(toolCallId: string): PendingPermission | undefined {
    return [...this.pending.values()].find((p) => p.toolCallId === toolCallId);
  }

  /** Escolhe uma opção pelo id. Devolve `false` se o pedido já foi resolvido ou a opção não existe. */
  select(permissionId: string, optionId: string): boolean {
    const entry = this.pending.get(permissionId);
    if (!entry || !entry.options.some((o) => o.optionId === optionId)) {
      return false;
    }
    this.finish(entry, { outcome: { outcome: 'selected', optionId } }, optionId);
    return true;
  }

  decide(permissionId: string, decision: Decision): boolean {
    const entry = this.pending.get(permissionId);
    if (!entry) {
      return false;
    }
    const option = pickOption(entry.options, decision);
    if (!option) {
      // Sem opção de rejeitar, a recusa vira cancelamento (o ACP permite).
      if (decision === 'reject') {
        return this.cancel(permissionId);
      }
      return false;
    }
    return this.select(permissionId, option.optionId);
  }

  cancel(permissionId: string): boolean {
    const entry = this.pending.get(permissionId);
    if (!entry) {
      return false;
    }
    this.finish(entry, { outcome: { outcome: 'cancelled' } }, undefined);
    return true;
  }

  /** Cancela todos os pedidos da sessão (o ACP exige isso após `session/cancel`). */
  cancelAll(sessionId?: string): void {
    for (const entry of [...this.pending.values()]) {
      if (!sessionId || entry.sessionId === sessionId) {
        this.finish(entry, { outcome: { outcome: 'cancelled' } }, undefined);
      }
    }
  }

  private finish(entry: Entry, response: RequestPermissionResponse, optionId: string | undefined): void {
    this.pending.delete(entry.permissionId);
    entry.resolve(response);
    for (const listener of this.resolvedListeners) {
      listener(entry.permissionId, optionId);
    }
  }
}
