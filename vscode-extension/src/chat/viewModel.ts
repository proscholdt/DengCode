/**
 * Modelo de visão do chat: transforma `session/update`, pedidos de permissão e
 * extensões `_dengcode/*` em itens que a webview desenha.
 *
 * Cada item tem `rev` (revisão): a webview só redesenha o que mudou.
 * Não depende do VS Code.
 */

import type {
  ContentBlock,
  DataPreviewParams,
  PermissionOption,
  PlanEntry,
  SessionUpdate,
  SqlApprovalParams,
  StopReason,
  ToolCallContent,
  ToolCallUpdate,
} from '../protocol/acpTypes';

export type ToolContentView =
  | { type: 'text'; text: string }
  | { type: 'diff'; path: string; isNewFile: boolean; added: number; removed: number }
  | { type: 'terminal'; terminalId: string }
  | { type: 'other'; label: string };

export interface PermissionView {
  permissionId: string;
  options: Array<Pick<PermissionOption, 'optionId' | 'name' | 'kind'>>;
  status: 'pending' | 'selected' | 'cancelled';
  selectedOptionId?: string;
}

export interface SqlCardView {
  sql: string;
  kind: string;
  engine?: string;
  statements?: number;
  tablesRead: string[];
  tablesWritten: string[];
  cost?: string;
  warnings: string[];
  plan?: string;
}

export interface DataPreviewView {
  columns: Array<{ name: string; type: string }>;
  rows: string[][];
  totalRows?: number;
  resultPath?: string;
}

export interface UserItem {
  kind: 'user';
  id: string;
  rev: number;
  text: string;
}

export interface AgentTextItem {
  kind: 'agent' | 'thought';
  id: string;
  rev: number;
  text: string;
}

export interface ToolItem {
  kind: 'tool';
  id: string;
  rev: number;
  toolCallId: string;
  title: string;
  toolKind: string;
  status: 'pending' | 'in_progress' | 'completed' | 'failed';
  content: ToolContentView[];
  locations: string[];
  sql?: SqlCardView;
  preview?: DataPreviewView;
  permission?: PermissionView;
}

export interface PlanItem {
  kind: 'plan';
  id: string;
  rev: number;
  entries: Array<{ content: string; status: string; priority: string }>;
}

export interface NoticeItem {
  kind: 'notice';
  id: string;
  rev: number;
  level: 'info' | 'warning' | 'error';
  text: string;
}

export type ChatItem = UserItem | AgentTextItem | ToolItem | PlanItem | NoticeItem;

export type EngineStatus = 'stopped' | 'starting' | 'ready' | 'error';

export interface ChatSnapshot {
  items: ChatItem[];
  busy: boolean;
  sessionId?: string;
  engineStatus: EngineStatus;
  engineMessage?: string;
}

const STOP_MESSAGES: Record<string, string | undefined> = {
  end_turn: undefined,
  cancelled: 'Turno cancelado.',
  max_tokens: 'O modelo atingiu o limite de tokens.',
  max_turn_requests: 'O turno atingiu o limite de passos.',
  refusal: 'O modelo recusou a tarefa.',
};

/** Converte qualquer valor em texto legível (custo e plano têm forma livre no contrato). */
export function stringifyFree(value: unknown): string | undefined {
  if (value === undefined || value === null || value === '') {
    return undefined;
  }
  if (typeof value === 'number') {
    return `US$ ${value.toFixed(value < 0.01 ? 4 : 2)}`;
  }
  if (typeof value === 'string') {
    return value;
  }
  if (typeof value === 'object' && !Array.isArray(value)) {
    const parts = Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v !== undefined && v !== null)
      .map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`);
    return parts.join(' · ');
  }
  return JSON.stringify(value, null, 2);
}

function planText(value: unknown): string | undefined {
  if (value === undefined || value === null || value === '') {
    return undefined;
  }
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

export function textOfContentBlock(block: ContentBlock | undefined): string {
  if (!block) {
    return '';
  }
  switch (block.type) {
    case 'text':
      return block.text;
    case 'resource_link':
      return `[${block.name}](${block.uri})`;
    case 'resource': {
      const resource = block.resource as { text?: string; uri?: string };
      return resource.text ?? resource.uri ?? '';
    }
    case 'image':
      return '[imagem]';
    case 'audio':
      return '[áudio]';
    default:
      return '';
  }
}

function countLines(text: string): number {
  if (text.length === 0) {
    return 0;
  }
  return text.split('\n').length - (text.endsWith('\n') ? 1 : 0);
}

/** Contagem simples de linhas adicionadas/removidas (multiconjunto de linhas). */
export function diffStats(oldText: string | null | undefined, newText: string): { added: number; removed: number } {
  const oldLines = (oldText ?? '').split('\n');
  const newLines = newText.split('\n');
  if (!oldText) {
    return { added: countLines(newText), removed: 0 };
  }
  const counts = new Map<string, number>();
  for (const line of oldLines) {
    counts.set(line, (counts.get(line) ?? 0) + 1);
  }
  let added = 0;
  for (const line of newLines) {
    const n = counts.get(line) ?? 0;
    if (n > 0) {
      counts.set(line, n - 1);
    } else {
      added++;
    }
  }
  let removed = 0;
  for (const n of counts.values()) {
    removed += n;
  }
  return { added, removed };
}

export function toolContentView(content: ToolCallContent): ToolContentView {
  switch (content.type) {
    case 'content':
      return { type: 'text', text: textOfContentBlock(content.content) };
    case 'diff': {
      const stats = diffStats(content.oldText, content.newText);
      return { type: 'diff', path: content.path, isNewFile: content.oldText == null, ...stats };
    }
    case 'terminal':
      return { type: 'terminal', terminalId: content.terminalId };
    default:
      return { type: 'other', label: (content as { type: string }).type };
  }
}

function cellText(value: unknown): string {
  if (value === null || value === undefined) {
    return 'NULL';
  }
  if (typeof value === 'object') {
    return JSON.stringify(value);
  }
  return String(value);
}

export function dataPreviewView(params: DataPreviewParams): DataPreviewView {
  const columns = (params.columns ?? []).map((c) => ({ name: String(c.name), type: String(c.type ?? '') }));
  const rows = (params.rows ?? []).slice(0, 20).map((row) => columns.map((c) => cellText(row?.[c.name])));
  const view: DataPreviewView = { columns, rows };
  if (typeof params.totalRows === 'number') {
    view.totalRows = params.totalRows;
  }
  if (params.resultPath) {
    view.resultPath = params.resultPath;
  }
  return view;
}

export function sqlCardView(params: SqlApprovalParams): SqlCardView {
  const view: SqlCardView = {
    sql: params.sql ?? '',
    kind: params.kind ?? 'UNKNOWN',
    tablesRead: params.tablesRead ?? [],
    tablesWritten: params.tablesWritten ?? [],
    warnings: params.warnings ?? [],
  };
  if (params.engine) {
    view.engine = params.engine;
  }
  if (typeof params.statements === 'number') {
    view.statements = params.statements;
  }
  const cost = stringifyFree(params.cost);
  if (cost) {
    view.cost = cost;
  }
  const plan = planText(params.plan);
  if (plan) {
    view.plan = plan;
  }
  return view;
}

export class ChatModel {
  private items: ChatItem[] = [];
  private seq = 0;
  private busy = false;
  private sessionId: string | undefined;
  private engineStatus: EngineStatus = 'stopped';
  private engineMessage: string | undefined;
  private readonly listeners: Array<() => void> = [];
  /** `_dengcode/sqlApproval` pode chegar antes do `tool_call`: guardamos até ele existir. */
  private readonly orphanSql = new Map<string, SqlCardView>();
  private readonly orphanPreview = new Map<string, DataPreviewView>();
  /** Itens de texto em streaming no turno atual (para emendar os chunks). */
  private openText: AgentTextItem | undefined;

  onChange(listener: () => void): void {
    this.listeners.push(listener);
  }

  snapshot(): ChatSnapshot {
    const snap: ChatSnapshot = { items: this.items, busy: this.busy, engineStatus: this.engineStatus };
    if (this.sessionId) {
      snap.sessionId = this.sessionId;
    }
    if (this.engineMessage) {
      snap.engineMessage = this.engineMessage;
    }
    return snap;
  }

  get isBusy(): boolean {
    return this.busy;
  }

  getTool(toolCallId: string): ToolItem | undefined {
    return this.items.find((i): i is ToolItem => i.kind === 'tool' && i.toolCallId === toolCallId);
  }

  /** Texto concatenado das respostas do agente (útil em testes e na exportação). */
  agentText(): string {
    return this.items
      .filter((i): i is AgentTextItem => i.kind === 'agent')
      .map((i) => i.text)
      .join('\n');
  }

  reset(sessionId?: string): void {
    this.items = [];
    this.openText = undefined;
    this.orphanSql.clear();
    this.orphanPreview.clear();
    this.busy = false;
    this.sessionId = sessionId;
    this.emit();
  }

  setSession(sessionId: string | undefined): void {
    this.sessionId = sessionId;
    this.emit();
  }

  setEngineStatus(status: EngineStatus, message?: string): void {
    this.engineStatus = status;
    this.engineMessage = message;
    this.emit();
  }

  addUserMessage(text: string): void {
    this.openText = undefined;
    this.push({ kind: 'user', id: this.nextId('u'), rev: 0, text });
    this.busy = true;
    this.emit();
  }

  addNotice(level: NoticeItem['level'], text: string): void {
    this.openText = undefined;
    this.push({ kind: 'notice', id: this.nextId('n'), rev: 0, level, text });
    this.emit();
  }

  endTurn(stopReason: StopReason | string): void {
    this.busy = false;
    this.openText = undefined;
    // Pedidos de permissão sem resposta ao fim do turno ficam como cancelados.
    for (const item of this.items) {
      if (item.kind === 'tool' && item.permission?.status === 'pending') {
        item.permission.status = 'cancelled';
        item.rev++;
      }
    }
    const message = STOP_MESSAGES[stopReason];
    if (message) {
      this.push({ kind: 'notice', id: this.nextId('n'), rev: 0, level: stopReason === 'cancelled' ? 'info' : 'warning', text: message });
    }
    this.emit();
  }

  failTurn(message: string): void {
    this.busy = false;
    this.openText = undefined;
    this.push({ kind: 'notice', id: this.nextId('n'), rev: 0, level: 'error', text: message });
    this.emit();
  }

  applyUpdate(update: SessionUpdate): void {
    switch (update.sessionUpdate) {
      case 'agent_message_chunk':
      case 'agent_thought_chunk':
        this.appendText(update.sessionUpdate === 'agent_message_chunk' ? 'agent' : 'thought', textOfContentBlock(update.content));
        break;
      case 'user_message_chunk':
        // Eco do prompt (ex.: ao carregar sessão). Mostramos como mensagem do usuário.
        this.openText = undefined;
        this.push({ kind: 'user', id: this.nextId('u'), rev: 0, text: textOfContentBlock(update.content) });
        break;
      case 'tool_call':
        this.upsertTool(update, true);
        break;
      case 'tool_call_update':
        this.upsertTool(update, false);
        break;
      case 'plan':
        this.setPlan(update.entries ?? []);
        break;
      default:
        // Outras atualizações (comandos disponíveis, modo atual, uso...) não aparecem no chat.
        return;
    }
    this.emit();
  }

  /** Registra (ou atualiza) a tool call do pedido de permissão e anexa as opções. */
  addPermission(permissionId: string, toolCall: ToolCallUpdate, options: PermissionOption[]): ToolItem {
    const tool = this.upsertTool(toolCall, false);
    tool.permission = {
      permissionId,
      options: options.map((o) => ({ optionId: o.optionId, name: o.name, kind: o.kind })),
      status: 'pending',
    };
    tool.rev++;
    this.emit();
    return tool;
  }

  resolvePermission(permissionId: string, optionId: string | undefined): void {
    for (const item of this.items) {
      if (item.kind === 'tool' && item.permission?.permissionId === permissionId) {
        item.permission.status = optionId ? 'selected' : 'cancelled';
        if (optionId) {
          item.permission.selectedOptionId = optionId;
        }
        item.rev++;
      }
    }
    this.emit();
  }

  applySqlApproval(params: SqlApprovalParams): void {
    const card = sqlCardView(params);
    const tool = this.getTool(params.toolCallId);
    if (tool) {
      tool.sql = card;
      tool.rev++;
      this.emit();
    } else {
      this.orphanSql.set(params.toolCallId, card);
    }
  }

  applyDataPreview(params: DataPreviewParams): void {
    const view = dataPreviewView(params);
    const tool = this.getTool(params.toolCallId);
    if (tool) {
      tool.preview = view;
      tool.rev++;
      this.emit();
    } else {
      this.orphanPreview.set(params.toolCallId, view);
    }
  }

  private appendText(kind: 'agent' | 'thought', text: string): void {
    if (text.length === 0) {
      return;
    }
    if (this.openText && this.openText.kind === kind && this.items[this.items.length - 1] === this.openText) {
      this.openText.text += text;
      this.openText.rev++;
      return;
    }
    const item: AgentTextItem = { kind, id: this.nextId(kind === 'agent' ? 'a' : 't'), rev: 0, text };
    this.push(item);
    this.openText = item;
  }

  private upsertTool(update: ToolCallUpdate & { title?: string | null }, isNew: boolean): ToolItem {
    let tool = this.getTool(update.toolCallId);
    if (!tool) {
      this.openText = undefined;
      tool = {
        kind: 'tool',
        id: this.nextId('c'),
        rev: 0,
        toolCallId: update.toolCallId,
        title: update.title ?? update.toolCallId,
        toolKind: update.kind ?? 'other',
        status: update.status ?? 'pending',
        content: [],
        locations: [],
      };
      const sql = this.orphanSql.get(update.toolCallId);
      if (sql) {
        tool.sql = sql;
        this.orphanSql.delete(update.toolCallId);
      }
      const preview = this.orphanPreview.get(update.toolCallId);
      if (preview) {
        tool.preview = preview;
        this.orphanPreview.delete(update.toolCallId);
      }
      this.push(tool);
    } else if (isNew) {
      // `tool_call` repetido: o ACP trata como substituição completa.
      tool.content = [];
      tool.locations = [];
    }
    if (update.title) {
      tool.title = update.title;
    }
    if (update.kind) {
      tool.toolKind = update.kind;
    }
    if (update.status) {
      tool.status = update.status;
    }
    // No ACP, `content` e `locations` de um update substituem os anteriores.
    if (update.content) {
      tool.content = update.content.map(toolContentView);
    }
    if (update.locations) {
      tool.locations = update.locations.map((l) => (l.line ? `${l.path}:${l.line}` : l.path));
    }
    tool.rev++;
    return tool;
  }

  private setPlan(entries: PlanEntry[]): void {
    const mapped = entries.map((e) => ({ content: e.content, status: e.status, priority: e.priority }));
    const existing = this.items.find((i): i is PlanItem => i.kind === 'plan');
    if (existing) {
      existing.entries = mapped;
      existing.rev++;
      return;
    }
    this.openText = undefined;
    this.push({ kind: 'plan', id: this.nextId('p'), rev: 0, entries: mapped });
  }

  private push(item: ChatItem): void {
    this.items.push(item);
  }

  private nextId(prefix: string): string {
    this.seq++;
    return `${prefix}${this.seq}`;
  }

  private emit(): void {
    for (const listener of this.listeners) {
      listener();
    }
  }
}
