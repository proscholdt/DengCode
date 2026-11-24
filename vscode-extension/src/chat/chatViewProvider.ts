/**
 * Webview do chat (view na barra lateral do DengCode).
 */

import * as vscode from 'vscode';
import { contentSecurityPolicy, escapeHtml, makeNonce } from '../render/html';
import { parseFromWebview, type FromWebviewMessage, type ToWebviewMessage } from './messages';
import type { ChatModel } from './viewModel';

export const CHAT_VIEW_ID = 'dengcode.chat';

export class ChatViewProvider implements vscode.WebviewViewProvider, vscode.Disposable {
  private view: vscode.WebviewView | undefined;
  private pendingTimer: ReturnType<typeof setTimeout> | undefined;
  private readonly disposables: vscode.Disposable[] = [];
  private renderedItems = -1;
  /** Erros de JavaScript relatados pela webview (diagnóstico e testes). */
  readonly webviewErrors: string[] = [];

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly model: ChatModel,
    private readonly onMessage: (message: FromWebviewMessage) => void,
  ) {
    model.onChange(() => this.schedulePost());
  }

  dispose(): void {
    for (const d of this.disposables) {
      d.dispose();
    }
  }

  get isResolved(): boolean {
    return this.view !== undefined;
  }

  /** Itens na tela segundo a última confirmação da webview (−1 = nunca desenhou). */
  get lastRenderedItems(): number {
    return this.renderedItems;
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const mediaRoot = vscode.Uri.joinPath(this.extensionUri, 'media');
    const distRoot = vscode.Uri.joinPath(this.extensionUri, 'dist', 'webview');
    view.webview.options = { enableScripts: true, localResourceRoots: [mediaRoot, distRoot] };
    view.webview.html = this.html(view.webview);
    this.disposables.push(
      view.webview.onDidReceiveMessage((raw: unknown) => {
        const message = parseFromWebview(raw);
        if (!message) {
          return;
        }
        if (message.type === 'ready') {
          this.postNow();
        } else if (message.type === 'rendered') {
          this.renderedItems = message.items;
          return;
        } else if (message.type === 'error') {
          this.webviewErrors.push(message.message);
        }
        this.onMessage(message);
      }),
      view.onDidChangeVisibility(() => {
        if (view.visible) {
          this.postNow();
        }
      }),
      view.onDidDispose(() => {
        this.view = undefined;
      }),
    );
  }

  /** Mostra a view (sem roubar o foco, por padrão). */
  reveal(preserveFocus = true): void {
    if (this.view) {
      this.view.show(preserveFocus);
    } else {
      void vscode.commands.executeCommand(`${CHAT_VIEW_ID}.focus`);
    }
  }

  focusInput(): void {
    this.post({ type: 'focusInput' });
  }

  private schedulePost(): void {
    // Junta rajadas de chunks de streaming num único repaint (~30 fps).
    if (this.pendingTimer) {
      return;
    }
    this.pendingTimer = setTimeout(() => {
      this.pendingTimer = undefined;
      this.postNow();
    }, 33);
  }

  private postNow(): void {
    this.post({ type: 'state', state: this.model.snapshot() });
  }

  private post(message: ToWebviewMessage): void {
    void this.view?.webview.postMessage(message);
  }

  private html(webview: vscode.Webview): string {
    const nonce = makeNonce();
    const script = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, 'dist', 'webview', 'chat.js'));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, 'media', 'chat.css'));
    const csp = contentSecurityPolicy(webview.cspSource, nonce);
    return `<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${escapeHtml(csp)}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${escapeHtml(style.toString())}">
<title>DengCode</title>
</head>
<body>
<div id="status" class="status" role="status" aria-live="polite"></div>
<main id="log" class="log" aria-live="polite"></main>
<form id="composer" class="composer">
  <textarea id="input" rows="3" placeholder="Pergunte ou peça uma tarefa de dados… (Enter envia, Shift+Enter quebra linha)" aria-label="Mensagem para o DengCode"></textarea>
  <div class="actions">
    <button type="button" id="new" class="secondary" title="Nova sessão">Nova sessão</button>
    <button type="button" id="cancel" class="secondary" title="Cancelar o turno atual" disabled>Cancelar</button>
    <button type="submit" id="send" title="Enviar">Enviar</button>
  </div>
</form>
<script nonce="${nonce}" src="${escapeHtml(script.toString())}"></script>
</body>
</html>`;
  }
}
