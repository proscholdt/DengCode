/**
 * Ponto de entrada da extensão DengCode (cliente ACP do motor `dengcode acp`).
 */

import * as path from 'node:path';
import * as vscode from 'vscode';
import { ChatViewProvider, CHAT_VIEW_ID } from './chat/chatViewProvider';
import { parseFromWebview, type FromWebviewMessage } from './chat/messages';
import { ChatModel } from './chat/viewModel';
import { readConfig } from './config';
import { DiffManager, PROPOSAL_SCHEME } from './diff/diffManager';
import { EngineManager } from './engine/engineManager';
import type { Logger } from './engine/engineProcess';
import { Panels } from './panels';
import type { ContentBlock, LineageDirection, WorkspaceEntry } from './protocol/acpTypes';
import { PermissionBroker } from './session/permissions';
import { SessionController } from './session/sessionController';
import { SqlCodeLensProvider } from './sql/sqlCodeLens';
import { buildSqlPrompt, type SqlAction } from './sql/sqlPrompt';
import { DengcodeStatusBar } from './statusBar';

/** API devolvida por `activate` — usada pelos testes de integração. Não é pública. */
export interface DengcodeApi {
  controller: SessionController;
  model: ChatModel;
  broker: PermissionBroker;
  diffs: DiffManager;
  panels: Panels;
  engines: EngineManager;
  chat: ChatViewProvider;
  handleWebviewMessage(message: unknown): Promise<void>;
}

function workspaceName(entry: WorkspaceEntry): string {
  return typeof entry === 'string' ? entry : entry.name;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function activate(context: vscode.ExtensionContext): DengcodeApi {
  const output = vscode.window.createOutputChannel('DengCode', { log: true });
  const logger: Logger = { info: (m) => output.info(m), error: (m) => output.error(m) };

  const model = new ChatModel();
  const broker = new PermissionBroker();
  const diffs = new DiffManager();
  const engines = new EngineManager(context, logger);
  const panels = new Panels(context.extensionUri);
  const statusBar = new DengcodeStatusBar();

  // `chat` é criado mais abaixo; o callback só roda depois da ativação.
  const controller = new SessionController(engines, model, diffs, broker, logger, {
    revealChat: () => chat.reveal(true),
  });
  statusBar.update(controller.activeContext);
  controller.onDidChangeActive((active) => statusBar.update(active));

  const handleWebviewMessage = async (message: FromWebviewMessage): Promise<void> => {
    switch (message.type) {
      case 'ready':
      case 'rendered':
        return;
      case 'error':
        logger.error(`webview: ${message.message}`);
        return;
      case 'prompt':
        await controller.sendPrompt(message.text);
        return;
      case 'cancel':
        await controller.cancel();
        return;
      case 'newSession':
        await runSafely(() => controller.newSession());
        return;
      case 'permission':
        if (message.optionId) {
          broker.select(message.permissionId, message.optionId);
        } else {
          broker.cancel(message.permissionId);
        }
        return;
      case 'openPreview': {
        const preview = model.getTool(message.toolCallId)?.preview;
        if (preview) {
          panels.showPreview(`Prévia: ${model.getTool(message.toolCallId)?.title ?? 'dados'}`, preview);
        }
        return;
      }
      case 'openDiff':
        await diffs.showForToolCall(message.toolCallId);
        return;
      case 'openLocation':
        await openLocation(message.location);
        return;
    }
  };

  const chat = new ChatViewProvider(context.extensionUri, model, (message) => {
    void handleWebviewMessage(message).catch((error: unknown) => logger.error(errorMessage(error)));
  });

  async function runSafely<T>(fn: () => Promise<T>): Promise<T | undefined> {
    try {
      return await fn();
    } catch (error) {
      const message = errorMessage(error);
      logger.error(message);
      void vscode.window.showErrorMessage(`DengCode: ${message}`);
      return undefined;
    }
  }

  async function openLocation(location: string): Promise<void> {
    const match = /^(.*?)(?::(\d+))?$/.exec(location);
    const file = match?.[1] ?? location;
    const line = match?.[2] ? Math.max(0, Number(match[2]) - 1) : 0;
    const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
    const absolute = path.isAbsolute(file) ? file : path.join(folder, file);
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(absolute));
    await vscode.window.showTextDocument(doc, { selection: new vscode.Range(line, 0, line, 0), preview: true });
  }

  async function sqlCommand(action: SqlAction, uri?: vscode.Uri): Promise<void> {
    const editor =
      uri && vscode.window.activeTextEditor?.document.uri.toString() !== uri.toString()
        ? await vscode.window.showTextDocument(uri, { preserveFocus: true })
        : vscode.window.activeTextEditor;
    if (!editor) {
      void vscode.window.showWarningMessage('DengCode: abra um arquivo SQL primeiro.');
      return;
    }
    const doc = editor.document;
    const selection = editor.selection;
    const isSelection = !selection.isEmpty;
    const sql = isSelection ? doc.getText(selection) : doc.getText();
    if (!sql.trim()) {
      void vscode.window.showWarningMessage('DengCode: o arquivo (ou a seleção) está vazio.');
      return;
    }
    const prompt = buildSqlPrompt({ action, fileLabel: vscode.workspace.asRelativePath(doc.uri), sql, isSelection });
    const link: ContentBlock[] = doc.uri.scheme === 'file' ? [{ type: 'resource_link', uri: doc.uri.toString(), name: path.basename(doc.uri.fsPath) }] : [];
    chat.reveal(true);
    await controller.sendPrompt(prompt, link);
  }

  async function switchStack(): Promise<void> {
    const list = await controller.stackList();
    const picked = await vscode.window.showQuickPick(
      list.stacks.map((s) => ({
        label: s.name,
        description: [s.name === list.active.stack ? '(ativa)' : '', s.origin ?? ''].filter(Boolean).join(' '),
        detail: s.description,
      })),
      { title: 'DengCode: trocar stack', placeHolder: `Atual: ${list.active.stack} · ${list.active.env}` },
    );
    if (!picked) {
      return;
    }
    const envs = Array.from(new Set(['dev', 'prod', list.active.env, readConfig().env]));
    const env = await vscode.window.showQuickPick(
      envs.map((e) => ({ label: e, description: e === list.active.env ? '(atual)' : e === 'prod' ? 'cuidado: produção' : '' })),
      { title: `DengCode: ambiente da stack ${picked.label}` },
    );
    if (!env) {
      return;
    }
    await controller.stackUse(picked.label, env.label);
    await offerNewSession(`stack ${picked.label} · ${env.label}`);
  }

  async function switchWorkspace(): Promise<void> {
    const list = await controller.workspaceList();
    const picked = await vscode.window.showQuickPick(
      list.workspaces.map(workspaceName).map((name) => ({ label: name, description: name === list.active ? '(ativo)' : '' })),
      { title: 'DengCode: trocar workspace', placeHolder: `Atual: ${list.active}` },
    );
    if (!picked) {
      return;
    }
    await controller.workspaceUse(picked.label);
    await offerNewSession(`workspace ${picked.label}`);
  }

  async function offerNewSession(what: string): Promise<void> {
    const choice = await vscode.window.showInformationMessage(
      `DengCode: ${what} vale para sessões novas.`,
      'Nova sessão agora',
    );
    if (choice) {
      await controller.newSession();
    }
  }

  async function showLineage(targetArg?: string): Promise<void> {
    let target = targetArg;
    if (!target) {
      const editor = vscode.window.activeTextEditor;
      let guess = '';
      if (editor) {
        guess = editor.document.getText(editor.selection).trim();
        if (!guess) {
          const range = editor.document.getWordRangeAtPosition(editor.selection.active, /[\w.]+/);
          guess = range ? editor.document.getText(range) : '';
        }
      }
      target = await vscode.window.showInputBox({
        title: 'DengCode: lineage',
        prompt: 'Tabela ou coluna (ex.: silver.pedidos ou silver.pedidos.valor)',
        value: guess,
      });
    }
    if (!target) {
      return;
    }
    const direction = await vscode.window.showQuickPick(
      [
        { label: 'Downstream', description: 'quem depende dela', value: 'down' as LineageDirection },
        { label: 'Upstream', description: 'de onde ela vem', value: 'up' as LineageDirection },
      ],
      { title: `DengCode: lineage de ${target}` },
    );
    if (!direction) {
      return;
    }
    const graph = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Window, title: `DengCode: lineage de ${target}` },
      () => controller.lineage(target, direction.value),
    );
    panels.showLineage(graph, direction.value);
  }

  function decideProposal(decision: 'accept' | 'reject', uri?: vscode.Uri): void {
    let target = uri;
    if (!target || target.scheme !== PROPOSAL_SCHEME) {
      const input = vscode.window.tabGroups.activeTabGroup.activeTab?.input;
      target = input instanceof vscode.TabInputTextDiff ? input.modified : undefined;
    }
    const permissionId = target ? diffs.permissionFor(target) : undefined;
    if (!permissionId || !broker.decide(permissionId, decision)) {
      void vscode.window.showInformationMessage('DengCode: esta proposta não está mais aguardando resposta.');
    }
  }

  const command = (id: string, fn: (...args: never[]) => Promise<unknown> | unknown): vscode.Disposable =>
    vscode.commands.registerCommand(id, (...args: never[]) => runSafely(async () => fn(...args)));

  context.subscriptions.push(
    output,
    diffs,
    statusBar,
    controller,
    chat,
    vscode.window.registerWebviewViewProvider(CHAT_VIEW_ID, chat, { webviewOptions: { retainContextWhenHidden: true } }),
    vscode.languages.registerCodeLensProvider([{ language: 'sql' }, { pattern: '**/*.sql' }], new SqlCodeLensProvider()),
    command('dengcode.novaSessao', async () => {
      chat.reveal(false);
      await controller.newSession();
      chat.focusInput();
    }),
    command('dengcode.executarSql', (uri?: vscode.Uri) => sqlCommand('executar', uri)),
    command('dengcode.explicarSql', (uri?: vscode.Uri) => sqlCommand('explicar', uri)),
    command('dengcode.trocarStack', () => switchStack()),
    command('dengcode.trocarWorkspace', () => switchWorkspace()),
    command('dengcode.mostrarLineage', (target?: string) => showLineage(typeof target === 'string' ? target : undefined)),
    command('dengcode.cancelar', () => controller.cancel()),
    command('dengcode.aceitarProposta', (uri?: vscode.Uri) => decideProposal('accept', uri)),
    command('dengcode.rejeitarProposta', (uri?: vscode.Uri) => decideProposal('reject', uri)),
    command('dengcode.reiniciarMotor', () => controller.restart()),
    command('dengcode.baixarMotor', () => engines.download()),
    command('dengcode.mostrarLog', () => output.show()),
    vscode.workspace.onDidChangeConfiguration(async (event) => {
      if (!event.affectsConfiguration('dengcode')) {
        return;
      }
      if (!controller.isRunning) {
        const config = readConfig();
        statusBar.update({ stack: config.stack || undefined, env: config.env, workspace: config.workspace || undefined });
        return;
      }
      const choice = await vscode.window.showInformationMessage(
        'DengCode: configurações alteradas. Reiniciar o motor para aplicar?',
        'Reiniciar',
      );
      if (choice) {
        await controller.restart();
      }
    }),
  );

  return {
    controller,
    model,
    broker,
    diffs,
    panels,
    engines,
    chat,
    handleWebviewMessage: async (raw: unknown) => {
      const message = parseFromWebview(raw);
      if (message) {
        await handleWebviewMessage(message);
      }
    },
  };
}

export function deactivate(): void {
  // Os recursos (incluindo o processo do motor) são liberados por context.subscriptions.
}
