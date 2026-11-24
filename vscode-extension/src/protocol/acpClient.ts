/**
 * Cliente ACP (lado do editor) sobre uma `JsonRpcConnection`.
 *
 * Responsabilidades: handshake (`initialize`), sessões, prompts, cancelamento,
 * despacho de `session/update`, pedidos de permissão e as extensões
 * `_dengcode/*`. Não conhece o VS Code: a interface se pluga pelos callbacks.
 */

import {
  ACP_PROTOCOL_VERSION,
  AcpMethods,
  DengcodeMethods,
  type ContentBlock,
  type DataPreviewParams,
  type DengcodeCapability,
  type DengcodeExtension,
  type InitializeRequest,
  type InitializeResponse,
  type LineageParams,
  type LineageResult,
  type NewSessionResponse,
  type PromptResponse,
  type RequestPermissionRequest,
  type RequestPermissionResponse,
  type SessionNotification,
  type SqlApprovalParams,
  type StackListResult,
  type StackUseResult,
  type WorkspaceListResult,
  type WorkspaceUseResult,
} from './acpTypes';
import { ErrorCodes, JsonRpcConnection, JsonRpcError } from './jsonrpc';

export interface AcpClientHandlers {
  onSessionUpdate?(notification: SessionNotification): void;
  onSqlApproval?(params: SqlApprovalParams): void;
  onDataPreview?(params: DataPreviewParams): void;
  /** Precisa sempre resolver: com a opção escolhida ou `cancelled`. */
  onRequestPermission?(request: RequestPermissionRequest): Promise<RequestPermissionResponse>;
  onUnknownNotification?(method: string, params: unknown): void;
}

export interface ClientInfo {
  name: string;
  title?: string;
  version: string;
}

/** Lê `agentCapabilities._meta.dengcode` de forma defensiva. */
export function readDengcodeCapability(response: InitializeResponse | undefined): DengcodeCapability | undefined {
  const meta = response?.agentCapabilities?._meta;
  const raw = meta && typeof meta === 'object' ? (meta as Record<string, unknown>).dengcode : undefined;
  if (!raw || typeof raw !== 'object') {
    return undefined;
  }
  const obj = raw as Record<string, unknown>;
  const extensions = Array.isArray(obj.extensions)
    ? (obj.extensions.filter((e) => typeof e === 'string') as DengcodeExtension[])
    : [];
  return { version: typeof obj.version === 'number' ? obj.version : 0, extensions };
}

export class AcpClient {
  private initializeResponse: InitializeResponse | undefined;
  private handlers: AcpClientHandlers = {};

  constructor(
    private readonly connection: JsonRpcConnection,
    private readonly clientInfo: ClientInfo,
  ) {
    connection.onNotification((method, params) => this.handleNotification(method, params));
    connection.onRequest((method, params) => this.handleRequest(method, params));
  }

  setHandlers(handlers: AcpClientHandlers): void {
    this.handlers = handlers;
  }

  get agentInfo(): InitializeResponse['agentInfo'] {
    return this.initializeResponse?.agentInfo;
  }

  get capabilities(): InitializeResponse | undefined {
    return this.initializeResponse;
  }

  get dengcode(): DengcodeCapability | undefined {
    return readDengcodeCapability(this.initializeResponse);
  }

  supports(extension: DengcodeExtension): boolean {
    return this.dengcode?.extensions.includes(extension) ?? false;
  }

  async initialize(timeoutMs = 30_000): Promise<InitializeResponse> {
    const request: InitializeRequest = {
      protocolVersion: ACP_PROTOCOL_VERSION,
      // A extensão não expõe sistema de arquivos nem terminal: o motor faz isso sozinho.
      clientCapabilities: { fs: { readTextFile: false, writeTextFile: false }, terminal: false },
      clientInfo: { name: this.clientInfo.name, title: this.clientInfo.title, version: this.clientInfo.version },
    };
    const response = await this.connection.request<InitializeResponse>(AcpMethods.initialize, request, { timeoutMs });
    if (response.protocolVersion !== ACP_PROTOCOL_VERSION) {
      throw new Error(
        `o motor fala ACP v${String(response.protocolVersion)}, mas esta extensão só suporta v${ACP_PROTOCOL_VERSION}`,
      );
    }
    this.initializeResponse = response;
    return response;
  }

  newSession(cwd: string): Promise<NewSessionResponse> {
    return this.connection.request<NewSessionResponse>(AcpMethods.sessionNew, { cwd, mcpServers: [] });
  }

  setMode(sessionId: string, modeId: string): Promise<unknown> {
    return this.connection.request(AcpMethods.sessionSetMode, { sessionId, modeId });
  }

  prompt(sessionId: string, prompt: ContentBlock[]): Promise<PromptResponse> {
    return this.connection.request<PromptResponse>(AcpMethods.sessionPrompt, { sessionId, prompt });
  }

  cancel(sessionId: string): void {
    this.connection.notify(AcpMethods.sessionCancel, { sessionId });
  }

  lineage(params: LineageParams): Promise<LineageResult> {
    return this.extRequest<LineageResult>('lineage', DengcodeMethods.lineage, params);
  }

  stackList(): Promise<StackListResult> {
    return this.extRequest<StackListResult>('stack', DengcodeMethods.stackList, {});
  }

  stackUse(stack: string, env: string): Promise<StackUseResult> {
    return this.extRequest<StackUseResult>('stack', DengcodeMethods.stackUse, { stack, env });
  }

  workspaceList(): Promise<WorkspaceListResult> {
    return this.extRequest<WorkspaceListResult>('workspace', DengcodeMethods.workspaceList, {});
  }

  workspaceUse(workspace: string): Promise<WorkspaceUseResult> {
    return this.extRequest<WorkspaceUseResult>('workspace', DengcodeMethods.workspaceUse, { workspace });
  }

  private extRequest<T>(extension: DengcodeExtension, method: string, params: unknown): Promise<T> {
    if (!this.supports(extension)) {
      return Promise.reject(new Error(`o motor não anuncia a extensão _dengcode/${extension}`));
    }
    return this.connection.request<T>(method, params, { timeoutMs: 60_000 });
  }

  private handleNotification(method: string, params: unknown): void {
    switch (method) {
      case AcpMethods.sessionUpdate:
        this.handlers.onSessionUpdate?.(params as SessionNotification);
        return;
      case DengcodeMethods.sqlApproval:
        this.handlers.onSqlApproval?.(params as SqlApprovalParams);
        return;
      case DengcodeMethods.dataPreview:
        this.handlers.onDataPreview?.(params as DataPreviewParams);
        return;
      default:
        // Extensões desconhecidas (prefixo `_`) são ignoradas, conforme o ACP.
        this.handlers.onUnknownNotification?.(method, params);
    }
  }

  private async handleRequest(method: string, params: unknown): Promise<unknown> {
    if (method === AcpMethods.sessionRequestPermission) {
      const handler = this.handlers.onRequestPermission;
      if (!handler) {
        return { outcome: { outcome: 'cancelled' } } satisfies RequestPermissionResponse;
      }
      return handler(params as RequestPermissionRequest);
    }
    // fs/*, terminal/* e afins não são anunciados nas capacidades do cliente.
    throw new JsonRpcError(ErrorCodes.methodNotFound, `método não suportado pelo cliente: ${method}`);
  }
}
