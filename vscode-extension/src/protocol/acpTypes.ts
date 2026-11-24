/**
 * Tipos do ACP (v1) e das extensões `_dengcode/*`.
 *
 * Os tipos do protocolo padrão vêm da biblioteca oficial
 * `@agentclientprotocol/sdk` **só em tempo de compilação** (`import type`):
 * nada dela entra no bundle. Os tipos das extensões seguem o contrato em
 * `docs/especificacoes/acp.md` ("Contrato das extensões `_dengcode/*`").
 */

export type {
  AgentCapabilities,
  ClientCapabilities,
  ContentBlock,
  Diff,
  InitializeRequest,
  InitializeResponse,
  NewSessionRequest,
  NewSessionResponse,
  PermissionOption,
  PermissionOptionKind,
  Plan,
  PlanEntry,
  PromptRequest,
  PromptResponse,
  RequestPermissionOutcome,
  RequestPermissionRequest,
  RequestPermissionResponse,
  SessionModeState,
  SessionNotification,
  SessionUpdate,
  StopReason,
  ToolCall,
  ToolCallContent,
  ToolCallLocation,
  ToolCallStatus,
  ToolCallUpdate,
  ToolKind,
} from '@agentclientprotocol/sdk';

/** Versão do ACP que este cliente fala (a biblioteca oficial exporta o mesmo valor). */
export const ACP_PROTOCOL_VERSION = 1;

/** Nomes dos métodos usados pelo cliente. */
export const AcpMethods = {
  initialize: 'initialize',
  sessionNew: 'session/new',
  sessionPrompt: 'session/prompt',
  sessionCancel: 'session/cancel',
  sessionSetMode: 'session/set_mode',
  sessionUpdate: 'session/update',
  sessionRequestPermission: 'session/request_permission',
} as const;

export const DengcodeMethods = {
  sqlApproval: '_dengcode/sqlApproval',
  dataPreview: '_dengcode/dataPreview',
  lineage: '_dengcode/lineage',
  stackList: '_dengcode/stack.list',
  stackUse: '_dengcode/stack.use',
  workspaceList: '_dengcode/workspace.list',
  workspaceUse: '_dengcode/workspace.use',
} as const;

export type DengcodeExtension = 'sqlApproval' | 'dataPreview' | 'lineage' | 'stack' | 'workspace';

/** `agentCapabilities._meta.dengcode` anunciado pelo motor no `initialize`. */
export interface DengcodeCapability {
  version: number;
  extensions: DengcodeExtension[];
}

/** `_dengcode/sqlApproval` — enviado logo antes do `session/request_permission` de SQL. */
export interface SqlApprovalParams {
  sessionId: string;
  toolCallId: string;
  sql: string;
  kind: string;
  engine?: string;
  statements?: number;
  tablesRead?: string[];
  tablesWritten?: string[];
  /** Forma livre: número (US$), texto ou objeto com estimativas. */
  cost?: unknown;
  warnings?: string[];
  /** Plano de execução (texto ou objeto), quando o motor o tiver. */
  plan?: unknown;
}

export interface DataPreviewColumn {
  name: string;
  type: string;
}

/** `_dengcode/dataPreview` — prévia tabular (≤ 20 linhas, já mascaradas). */
export interface DataPreviewParams {
  sessionId: string;
  toolCallId: string;
  columns: DataPreviewColumn[];
  rows: Array<Record<string, unknown>>;
  totalRows?: number | null;
  resultPath?: string | null;
}

export type LineageDirection = 'down' | 'up';

export interface LineageParams {
  sessionId: string;
  target: string;
  direction: LineageDirection;
  depth: number;
}

export interface LineageNode {
  id: string;
  kind: 'table' | 'column' | string;
}

export interface LineageEdge {
  source: string;
  target: string;
  via?: string | null;
}

export interface LineageResult {
  root: string;
  nodes: LineageNode[];
  edges: LineageEdge[];
}

export interface StackInfo {
  name: string;
  origin?: string;
  description?: string;
}

export interface StackListResult {
  stacks: StackInfo[];
  active: { stack: string; env: string };
}

export interface StackUseResult {
  stack: string;
  env: string;
}

/** O contrato não fixa a forma de cada workspace: aceitamos nome ou objeto com `name`. */
export type WorkspaceEntry = string | { name: string; [key: string]: unknown };

export interface WorkspaceListResult {
  workspaces: WorkspaceEntry[];
  active: string;
}

export interface WorkspaceUseResult {
  workspace: string;
}
