/**
 * Orquestra motor, cliente ACP, sessão, modelo do chat, permissões e diffs.
 */

import * as vscode from 'vscode';
import { projectFolder, readConfig } from '../config';
import type { ChatModel } from '../chat/viewModel';
import type { DiffManager } from '../diff/diffManager';
import { EngineProcess, type Logger } from '../engine/engineProcess';
import type { EngineManager } from '../engine/engineManager';
import { settingsEnv } from '../engine/spawnSpec';
import { AcpClient } from '../protocol/acpClient';
import type {
  ContentBlock,
  Diff,
  LineageDirection,
  LineageResult,
  NewSessionResponse,
  RequestPermissionRequest,
  RequestPermissionResponse,
  SessionNotification,
  StackListResult,
  ToolCallContent,
  WorkspaceListResult,
} from '../protocol/acpTypes';
import type { PermissionBroker } from './permissions';

export interface ActiveContext {
  stack?: string;
  env?: string;
  workspace?: string;
}

export function diffsOf(content: ToolCallContent[] | null | undefined): Diff[] {
  return (content ?? []).filter((c): c is Diff & { type: 'diff' } => c.type === 'diff');
}

export class SessionController implements vscode.Disposable {
  private process: EngineProcess | undefined;
  private client: AcpClient | undefined;
  private starting: Promise<AcpClient> | undefined;
  private sessionId: string | undefined;
  private sessionCwd: string | undefined;
  private readonly rawDiffs = new Map<string, Diff[]>();
  private active: ActiveContext = {};
  private readonly activeEmitter = new vscode.EventEmitter<ActiveContext>();
  readonly onDidChangeActive = this.activeEmitter.event;
  private lastSessionResponse: NewSessionResponse | undefined;

  constructor(
    private readonly engines: EngineManager,
    readonly model: ChatModel,
    private readonly diffs: DiffManager,
    private readonly broker: PermissionBroker,
    private readonly logger: Logger,
    private readonly hooks: { revealChat(): void } = { revealChat: () => undefined },
  ) {
    broker.onResolved((permissionId, optionId) => {
      model.resolvePermission(permissionId, optionId);
      void diffs.close(permissionId);
    });
    const config = readConfig();
    this.active = { stack: config.stack || undefined, env: config.env, workspace: config.workspace || undefined };
  }

  get currentSessionId(): string | undefined {
    return this.sessionId;
  }

  get activeContext(): ActiveContext {
    return this.active;
  }

  get isRunning(): boolean {
    return this.client !== undefined;
  }

  get agentClient(): AcpClient | undefined {
    return this.client;
  }

  get newSessionResponse(): NewSessionResponse | undefined {
    return this.lastSessionResponse;
  }

  dispose(): void {
    void this.stop();
    this.activeEmitter.dispose();
  }

  /** Inicia o motor (uma vez) e faz o handshake. */
  ensureClient(): Promise<AcpClient> {
    if (this.client) {
      return Promise.resolve(this.client);
    }
    this.starting ??= this.start().finally(() => {
      this.starting = undefined;
    });
    return this.starting;
  }

  private async start(): Promise<AcpClient> {
    this.model.setEngineStatus('starting', 'Iniciando o motor…');
    const engine = await this.engines.resolve();
    if (!engine) {
      this.model.setEngineStatus('error', 'Motor do DengCode não encontrado.');
      throw new Error('motor do DengCode não encontrado');
    }
    const cwd = projectFolder();
    if (!cwd) {
      this.model.setEngineStatus('error', 'Abra uma pasta para usar o DengCode.');
      throw new Error('abra uma pasta (workspace) para usar o DengCode');
    }
    const config = readConfig();
    const proc = EngineProcess.start(engine, { cwd, env: settingsEnv(config), logger: this.logger });
    const client = new AcpClient(proc.connection, {
      name: 'dengcode-vscode',
      title: 'DengCode para VS Code',
      version: this.engines.extensionVersion,
    });
    client.setHandlers({
      onSessionUpdate: (n) => this.handleUpdate(n),
      onSqlApproval: (p) => {
        if (this.isCurrent(p.sessionId)) {
          this.model.applySqlApproval(p);
        }
      },
      onDataPreview: (p) => {
        if (this.isCurrent(p.sessionId)) {
          this.model.applyDataPreview(p);
        }
      },
      onRequestPermission: (r) => this.handlePermission(r),
      onUnknownNotification: (method) => this.logger.info(`notificação ignorada: ${method}`),
    });
    proc.onExit((code) => {
      if (this.process !== proc) {
        return;
      }
      this.process = undefined;
      this.client = undefined;
      this.sessionId = undefined;
      this.broker.cancelAll();
      const wasBusy = this.model.isBusy;
      this.model.setSession(undefined);
      this.model.setEngineStatus('error', `O motor encerrou (código ${String(code)}). Envie uma mensagem para reiniciar.`);
      if (wasBusy) {
        this.model.failTurn('O motor encerrou durante o turno.');
      }
    });
    this.process = proc;
    try {
      const init = await client.initialize();
      const agent = init.agentInfo ? `${init.agentInfo.name} ${init.agentInfo.version ?? ''}`.trim() : 'motor';
      this.logger.info(`ACP pronto: ${agent}; extensões: ${client.dengcode?.extensions.join(', ') ?? 'nenhuma'}`);
    } catch (error) {
      this.process = undefined;
      await proc.stop(500);
      const message = `Falha no handshake ACP: ${(error as Error).message}`;
      this.model.setEngineStatus('error', message);
      throw new Error(message);
    }
    this.client = client;
    await this.applyPreferences(client, config);
    this.model.setEngineStatus('ready');
    return client;
  }

  /** Aplica stack/workspace das configurações via extensões e lê o contexto ativo. */
  private async applyPreferences(client: AcpClient, config: ReturnType<typeof readConfig>): Promise<void> {
    if (client.supports('stack')) {
      try {
        if (config.stack) {
          await client.stackUse(config.stack, config.env);
        }
        const list = await client.stackList();
        this.active = { ...this.active, stack: list.active.stack, env: list.active.env };
      } catch (error) {
        this.logger.error(`não foi possível aplicar a stack: ${(error as Error).message}`);
      }
    }
    if (client.supports('workspace')) {
      try {
        if (config.workspace) {
          await client.workspaceUse(config.workspace);
        }
        const list = await client.workspaceList();
        this.active = { ...this.active, workspace: list.active };
      } catch (error) {
        this.logger.error(`não foi possível aplicar o workspace: ${(error as Error).message}`);
      }
    }
    this.activeEmitter.fire(this.active);
  }

  async ensureSession(): Promise<string> {
    const client = await this.ensureClient();
    if (this.sessionId) {
      return this.sessionId;
    }
    const cwd = projectFolder() ?? '';
    const response = await client.newSession(cwd);
    this.lastSessionResponse = response;
    this.sessionId = response.sessionId;
    this.sessionCwd = cwd;
    this.model.setSession(response.sessionId);
    await this.applyMode(client, response);
    this.logger.info(`sessão ${response.sessionId} criada em ${this.sessionCwd}`);
    return response.sessionId;
  }

  /** Se o motor expõe modos ACP e um deles é o modo configurado, seleciona-o. */
  private async applyMode(client: AcpClient, response: NewSessionResponse): Promise<void> {
    const mode = readConfig().permissionMode;
    const modes = response.modes;
    if (!modes || modes.currentModeId === mode || !modes.availableModes.some((m) => m.id === mode)) {
      return;
    }
    try {
      await client.setMode(response.sessionId, mode);
    } catch (error) {
      this.logger.error(`não foi possível trocar o modo para ${mode}: ${(error as Error).message}`);
    }
  }

  /** Começa uma conversa nova (o motor continua o mesmo). */
  async newSession(): Promise<string> {
    if (this.sessionId && this.model.isBusy) {
      await this.cancel();
    }
    this.sessionId = undefined;
    this.rawDiffs.clear();
    this.model.reset();
    return this.ensureSession();
  }

  async sendPrompt(text: string, extra: ContentBlock[] = []): Promise<void> {
    if (this.model.isBusy) {
      void vscode.window.showWarningMessage('DengCode: aguarde o turno atual terminar ou cancele.');
      return;
    }
    this.model.addUserMessage(text);
    let sessionId: string;
    try {
      sessionId = await this.ensureSession();
    } catch (error) {
      this.model.failTurn((error as Error).message);
      return;
    }
    const client = this.client;
    if (!client) {
      this.model.failTurn('O motor não está disponível.');
      return;
    }
    try {
      const response = await client.prompt(sessionId, [{ type: 'text', text }, ...extra]);
      this.model.endTurn(response.stopReason);
    } catch (error) {
      this.model.failTurn(`Erro no turno: ${(error as Error).message}`);
    }
  }

  async cancel(): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId || !this.client) {
      return;
    }
    // O ACP exige responder os pedidos de permissão pendentes como `cancelled`.
    this.broker.cancelAll(sessionId);
    this.client.cancel(sessionId);
  }

  async lineage(target: string, direction: LineageDirection, depth = 3): Promise<LineageResult> {
    const sessionId = await this.ensureSession();
    const client = await this.ensureClient();
    return client.lineage({ sessionId, target, direction, depth });
  }

  async stackList(): Promise<StackListResult> {
    return (await this.ensureClient()).stackList();
  }

  async stackUse(stack: string, env: string): Promise<void> {
    const result = await (await this.ensureClient()).stackUse(stack, env);
    this.active = { ...this.active, stack: result.stack, env: result.env };
    this.activeEmitter.fire(this.active);
  }

  async workspaceList(): Promise<WorkspaceListResult> {
    return (await this.ensureClient()).workspaceList();
  }

  async workspaceUse(workspace: string): Promise<void> {
    const result = await (await this.ensureClient()).workspaceUse(workspace);
    this.active = { ...this.active, workspace: result.workspace };
    this.activeEmitter.fire(this.active);
  }

  async stop(): Promise<void> {
    const proc = this.process;
    this.process = undefined;
    this.client = undefined;
    this.sessionId = undefined;
    this.broker.cancelAll();
    if (proc) {
      await proc.stop();
    }
    this.model.setEngineStatus('stopped');
  }

  /** Para o motor e o inicia de novo na próxima mensagem (ex.: após mudar a configuração). */
  async restart(): Promise<void> {
    await this.stop();
    this.model.reset();
  }

  private isCurrent(sessionId: string | undefined): boolean {
    if (sessionId && this.sessionId && sessionId !== this.sessionId) {
      this.logger.info(`mensagem de outra sessão ignorada: ${sessionId}`);
      return false;
    }
    return true;
  }

  private handleUpdate(notification: SessionNotification): void {
    if (!this.isCurrent(notification.sessionId)) {
      return;
    }
    const update = notification.update;
    if (update.sessionUpdate === 'tool_call' || update.sessionUpdate === 'tool_call_update') {
      const found = diffsOf(update.content);
      if (found.length > 0) {
        this.rawDiffs.set(update.toolCallId, found);
        // Guarda para o botão "Ver diff" do chat (o conteúdo mais recente vence).
        this.diffs.register(update.toolCallId, found);
      }
    }
    this.model.applyUpdate(update);
  }

  private async handlePermission(request: RequestPermissionRequest): Promise<RequestPermissionResponse> {
    if (!this.isCurrent(request.sessionId)) {
      return { outcome: { outcome: 'cancelled' } };
    }
    const toolCallId = request.toolCall.toolCallId;
    const { permissionId, response } = this.broker.open(request.sessionId, toolCallId, request.options);
    const tool = this.model.addPermission(permissionId, request.toolCall, request.options);
    const requestDiffs = diffsOf(request.toolCall.content);
    const diffs = requestDiffs.length > 0 ? requestDiffs : (this.rawDiffs.get(toolCallId) ?? []);
    if (diffs.length > 0) {
      this.diffs.register(toolCallId, diffs, permissionId);
      void this.diffs.show(permissionId).catch((error: unknown) => this.logger.error(`falha ao abrir diff: ${String(error)}`));
      const files = diffs.map((d) => d.path.split(/[\\/]/).pop()).join(', ');
      void vscode.window
        .showInformationMessage(`DengCode propõe alterar ${files}.`, 'Aceitar', 'Rejeitar')
        .then((choice) => {
          if (choice === 'Aceitar') {
            this.broker.decide(permissionId, 'accept');
          } else if (choice === 'Rejeitar') {
            this.broker.decide(permissionId, 'reject');
          }
        });
    } else {
      this.hooks.revealChat();
    }
    this.logger.info(`permissão pedida: ${tool.title} (${permissionId})`);
    return response;
  }
}
