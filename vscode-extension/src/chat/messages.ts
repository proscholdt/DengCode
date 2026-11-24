/**
 * Mensagens trocadas entre a webview do chat e a extensão.
 * Compartilhado pelos dois lados; a extensão valida tudo o que chega.
 */

import type { ChatSnapshot } from './viewModel';

export type ToWebviewMessage = { type: 'state'; state: ChatSnapshot } | { type: 'focusInput' };

export type FromWebviewMessage =
  | { type: 'ready' }
  | { type: 'prompt'; text: string }
  | { type: 'cancel' }
  | { type: 'newSession' }
  | { type: 'permission'; permissionId: string; optionId: string | null }
  | { type: 'openPreview'; toolCallId: string }
  | { type: 'openDiff'; toolCallId: string }
  | { type: 'openLocation'; location: string }
  /** Confirmação de desenho (diagnóstico e testes): quantos itens estão na tela. */
  | { type: 'rendered'; items: number }
  /** Erro de JavaScript dentro da webview, repassado para o log. */
  | { type: 'error'; message: string };

function isString(value: unknown, maxLength = 1_000_000): value is string {
  return typeof value === 'string' && value.length <= maxLength;
}

/** Valida uma mensagem vinda da webview (conteúdo não confiável). */
export function parseFromWebview(value: unknown): FromWebviewMessage | undefined {
  if (typeof value !== 'object' || value === null) {
    return undefined;
  }
  const msg = value as Record<string, unknown>;
  switch (msg.type) {
    case 'ready':
    case 'cancel':
    case 'newSession':
      return { type: msg.type };
    case 'prompt':
      return isString(msg.text) && msg.text.trim().length > 0 ? { type: 'prompt', text: msg.text } : undefined;
    case 'permission':
      if (!isString(msg.permissionId, 200)) {
        return undefined;
      }
      if (msg.optionId !== null && !isString(msg.optionId, 200)) {
        return undefined;
      }
      return { type: 'permission', permissionId: msg.permissionId, optionId: msg.optionId as string | null };
    case 'openPreview':
    case 'openDiff':
      return isString(msg.toolCallId, 200) ? { type: msg.type, toolCallId: msg.toolCallId } : undefined;
    case 'openLocation':
      return isString(msg.location, 4096) ? { type: 'openLocation', location: msg.location } : undefined;
    case 'rendered':
      return typeof msg.items === 'number' && Number.isInteger(msg.items) && msg.items >= 0
        ? { type: 'rendered', items: msg.items }
        : undefined;
    case 'error':
      return isString(msg.message, 4000) ? { type: 'error', message: msg.message } : undefined;
    default:
      return undefined;
  }
}
