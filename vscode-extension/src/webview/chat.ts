/**
 * Script da webview do chat (roda no navegador da webview, sem acesso a Node).
 */

import type { FromWebviewMessage, ToWebviewMessage } from '../chat/messages';
import type { ChatSnapshot } from '../chat/viewModel';
import { renderItem } from './renderItems';

interface VsCodeApi {
  postMessage(message: FromWebviewMessage): void;
}

declare function acquireVsCodeApi(): VsCodeApi;

const vscode = acquireVsCodeApi();
const log = document.getElementById('log') as HTMLElement;
const status = document.getElementById('status') as HTMLElement;
const form = document.getElementById('composer') as HTMLFormElement;
const input = document.getElementById('input') as HTMLTextAreaElement;
const sendButton = document.getElementById('send') as HTMLButtonElement;
const cancelButton = document.getElementById('cancel') as HTMLButtonElement;
const newButton = document.getElementById('new') as HTMLButtonElement;

const rendered = new Map<string, { rev: number; el: HTMLElement }>();
/** Estado aberto/fechado escolhido pelo usuário em cada `<details>`. */
const userOpen = new Map<string, boolean>();

function post(message: FromWebviewMessage): void {
  vscode.postMessage(message);
}

function nearBottom(): boolean {
  return log.scrollHeight - log.scrollTop - log.clientHeight < 60;
}

function render(state: ChatSnapshot): void {
  const stick = nearBottom();
  const seen = new Set<string>();
  let previous: Element | null = null;
  for (const item of state.items) {
    seen.add(item.id);
    let entry = rendered.get(item.id);
    if (!entry || entry.rev !== item.rev) {
      const el = document.createElement('div');
      el.className = 'item';
      el.dataset.id = item.id;
      el.innerHTML = renderItem(item);
      const details = el.querySelector('details.tool');
      const forced = details?.hasAttribute('open') ?? false;
      const chosen = userOpen.get(item.id);
      if (details && chosen !== undefined && !forced) {
        (details as HTMLDetailsElement).open = chosen;
      }
      if (entry) {
        entry.el.replaceWith(el);
      } else if (previous) {
        previous.after(el);
      } else {
        log.prepend(el);
      }
      entry = { rev: item.rev, el };
      rendered.set(item.id, entry);
    }
    previous = entry.el;
  }
  for (const [id, entry] of rendered) {
    if (!seen.has(id)) {
      entry.el.remove();
      rendered.delete(id);
      userOpen.delete(id);
    }
  }
  if (state.items.length === 0) {
    log.innerHTML = '<div class="empty">Converse com o DengCode sobre seus dados: schemas, consultas, pipelines e lineage.</div>';
    rendered.clear();
  } else {
    log.querySelector('.empty')?.remove();
  }

  const statusText: Record<ChatSnapshot['engineStatus'], string> = {
    stopped: '',
    starting: 'Iniciando o motor…',
    ready: state.busy ? 'Trabalhando…' : '',
    error: state.engineMessage ?? 'Erro no motor.',
  };
  status.textContent = state.engineStatus === 'ready' ? statusText.ready : (state.engineMessage ?? statusText[state.engineStatus]);
  status.className = `status ${state.engineStatus}${state.busy ? ' busy' : ''}`;
  sendButton.disabled = state.busy;
  cancelButton.disabled = !state.busy;
  if (stick) {
    log.scrollTop = log.scrollHeight;
  }
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text || sendButton.disabled) {
    return;
  }
  post({ type: 'prompt', text });
  input.value = '';
});

input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.requestSubmit();
  }
});

cancelButton.addEventListener('click', () => post({ type: 'cancel' }));
newButton.addEventListener('click', () => post({ type: 'newSession' }));

log.addEventListener(
  'toggle',
  (event) => {
    const details = event.target as HTMLElement;
    if (details instanceof HTMLDetailsElement && details.classList.contains('tool')) {
      const id = (details.closest('.item') as HTMLElement | null)?.dataset.id;
      if (id) {
        userOpen.set(id, details.open);
      }
    }
  },
  true,
);

log.addEventListener('click', (event) => {
  const target = (event.target as HTMLElement).closest('button[data-action]') as HTMLButtonElement | null;
  if (!target) {
    return;
  }
  const action = target.dataset.action;
  if (action === 'permission') {
    // Desabilita para evitar clique duplo enquanto a extensão processa.
    target.closest('.permission')?.querySelectorAll('button').forEach((b) => ((b as HTMLButtonElement).disabled = true));
    post({ type: 'permission', permissionId: target.dataset.permission ?? '', optionId: target.dataset.option ?? null });
  } else if (action === 'openDiff' && target.dataset.tool) {
    post({ type: 'openDiff', toolCallId: target.dataset.tool });
  } else if (action === 'openPreview' && target.dataset.tool) {
    post({ type: 'openPreview', toolCallId: target.dataset.tool });
  } else if (action === 'openLocation' && target.dataset.location) {
    post({ type: 'openLocation', location: target.dataset.location });
  }
});

window.addEventListener('message', (event: MessageEvent<ToWebviewMessage>) => {
  const message = event.data;
  if (message.type === 'state') {
    try {
      render(message.state);
      post({ type: 'rendered', items: message.state.items.length });
    } catch (error) {
      post({ type: 'error', message: `falha ao desenhar o chat: ${String(error)}` });
    }
  } else if (message.type === 'focusInput') {
    input.focus();
  }
});

window.addEventListener('error', (event) => post({ type: 'error', message: String(event.message) }));

post({ type: 'ready' });
