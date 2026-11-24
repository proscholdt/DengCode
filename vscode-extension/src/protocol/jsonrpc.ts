/**
 * Cliente JSON-RPC 2.0 mínimo sobre NDJSON (uma mensagem JSON por linha).
 *
 * É o transporte do ACP: o motor (`dengcode acp`) lê do stdin e escreve no
 * stdout. Este módulo não conhece o VS Code nem o ACP — só o enquadramento e o
 * casamento de requisições/respostas — para poder ser testado isoladamente.
 */

export type JsonRpcId = number | string;

export interface JsonRpcRequest {
  jsonrpc: '2.0';
  id: JsonRpcId;
  method: string;
  params?: unknown;
}

export interface JsonRpcNotification {
  jsonrpc: '2.0';
  method: string;
  params?: unknown;
}

export interface JsonRpcErrorObject {
  code: number;
  message: string;
  data?: unknown;
}

export interface JsonRpcResponse {
  jsonrpc: '2.0';
  id: JsonRpcId | null;
  result?: unknown;
  error?: JsonRpcErrorObject;
}

export type JsonRpcMessage = JsonRpcRequest | JsonRpcNotification | JsonRpcResponse;

/** Códigos padrão do JSON-RPC 2.0. */
export const ErrorCodes = {
  parseError: -32700,
  invalidRequest: -32600,
  methodNotFound: -32601,
  invalidParams: -32602,
  internalError: -32603,
} as const;

/** Erro devolvido pelo outro lado (ou gerado localmente) numa requisição. */
export class JsonRpcError extends Error {
  readonly code: number;
  readonly data: unknown;

  constructor(code: number, message: string, data?: unknown) {
    super(message);
    this.name = 'JsonRpcError';
    this.code = code;
    this.data = data;
  }

  toObject(): JsonRpcErrorObject {
    const obj: JsonRpcErrorObject = { code: this.code, message: this.message };
    if (this.data !== undefined) {
      obj.data = this.data;
    }
    return obj;
  }
}

/** A conexão foi encerrada (motor saiu, cancelamento, etc.). */
export class ConnectionClosedError extends Error {
  constructor(message = 'conexão com o motor encerrada') {
    super(message);
    this.name = 'ConnectionClosedError';
  }
}

/**
 * Junta pedaços de bytes/texto em linhas completas. Aceita `\n` e `\r\n`,
 * preserva caracteres UTF-8 quebrados entre pedaços e ignora linhas vazias.
 */
export class LineDecoder {
  private buffer = '';
  private readonly decoder = new TextDecoder('utf-8');

  constructor(private readonly maxLineBytes = 64 * 1024 * 1024) {}

  push(chunk: Uint8Array | string): string[] {
    this.buffer += typeof chunk === 'string' ? chunk : this.decoder.decode(chunk, { stream: true });
    const lines: string[] = [];
    let index = this.buffer.indexOf('\n');
    while (index >= 0) {
      const line = this.buffer.slice(0, index).replace(/\r$/, '');
      this.buffer = this.buffer.slice(index + 1);
      if (line.trim().length > 0) {
        lines.push(line);
      }
      index = this.buffer.indexOf('\n');
    }
    if (this.buffer.length > this.maxLineBytes) {
      this.buffer = '';
      throw new Error('mensagem do motor excede o tamanho máximo permitido');
    }
    return lines;
  }

  /** Devolve o que sobrou sem `\n` final (fim do stream). */
  flush(): string[] {
    const rest = (this.buffer + this.decoder.decode()).trim();
    this.buffer = '';
    return rest.length > 0 ? [rest] : [];
  }
}

/** Serializa uma mensagem como uma linha NDJSON. */
export function encodeMessage(message: JsonRpcMessage): string {
  // JSON.stringify já escapa `\n` dentro de strings, então a linha é única.
  return `${JSON.stringify(message)}\n`;
}

/** Classifica um objeto já desserializado. Devolve `undefined` se não for JSON-RPC válido. */
export function classifyMessage(
  value: unknown,
):
  | { kind: 'request'; message: JsonRpcRequest }
  | { kind: 'notification'; message: JsonRpcNotification }
  | { kind: 'response'; message: JsonRpcResponse }
  | undefined {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return undefined;
  }
  const obj = value as Record<string, unknown>;
  if (obj.jsonrpc !== '2.0') {
    return undefined;
  }
  const hasId = 'id' in obj && (typeof obj.id === 'number' || typeof obj.id === 'string');
  if (typeof obj.method === 'string') {
    return hasId
      ? { kind: 'request', message: obj as unknown as JsonRpcRequest }
      : { kind: 'notification', message: obj as unknown as JsonRpcNotification };
  }
  if (('result' in obj || 'error' in obj) && (hasId || obj.id === null)) {
    return { kind: 'response', message: obj as unknown as JsonRpcResponse };
  }
  return undefined;
}

export type RequestHandler = (method: string, params: unknown) => Promise<unknown>;
export type NotificationHandler = (method: string, params: unknown) => void;

export interface RequestOptions {
  /** Cancela a espera local (a requisição não é desfeita no outro lado). */
  signal?: AbortSignal;
  /** Tempo máximo de espera, em ms. Sem valor = espera indefinida. */
  timeoutMs?: number;
}

interface Pending {
  method: string;
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  cleanup: () => void;
}

/**
 * Conexão JSON-RPC bidirecional: envia requisições/notificações e despacha as
 * que chegam para os handlers. A escrita é injetada (`write`) e a leitura é
 * alimentada por `receive()`, então funciona com stdio, streams em memória ou
 * testes.
 */
export class JsonRpcConnection {
  private nextId = 1;
  private readonly pending = new Map<JsonRpcId, Pending>();
  private readonly decoder = new LineDecoder();
  private closedError: Error | undefined;
  private requestHandler: RequestHandler | undefined;
  private notificationHandler: NotificationHandler | undefined;
  private readonly closeListeners: Array<(error: Error) => void> = [];

  constructor(
    private readonly write: (line: string) => void,
    private readonly log: (message: string) => void = () => undefined,
  ) {}

  get isClosed(): boolean {
    return this.closedError !== undefined;
  }

  onRequest(handler: RequestHandler): void {
    this.requestHandler = handler;
  }

  onNotification(handler: NotificationHandler): void {
    this.notificationHandler = handler;
  }

  onClose(listener: (error: Error) => void): void {
    this.closeListeners.push(listener);
  }

  /** Alimenta a conexão com bytes/texto lidos do outro lado. */
  receive(chunk: Uint8Array | string): void {
    let lines: string[];
    try {
      lines = this.decoder.push(chunk);
    } catch (error) {
      this.log(`descartando entrada: ${(error as Error).message}`);
      return;
    }
    for (const line of lines) {
      this.handleLine(line);
    }
  }

  /** Fim da entrada: processa o resto e fecha. */
  end(reason?: Error): void {
    for (const line of this.decoder.flush()) {
      this.handleLine(line);
    }
    this.close(reason ?? new ConnectionClosedError());
  }

  request<T = unknown>(method: string, params?: unknown, options: RequestOptions = {}): Promise<T> {
    if (this.closedError) {
      return Promise.reject(this.closedError);
    }
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const onAbort = (): void => {
        this.pending.delete(id);
        cleanup();
        reject(new JsonRpcError(-32800, `requisição ${method} cancelada`));
      };
      const cleanup = (): void => {
        if (timer) {
          clearTimeout(timer);
        }
        options.signal?.removeEventListener('abort', onAbort);
      };
      if (options.signal?.aborted) {
        reject(new JsonRpcError(-32800, `requisição ${method} cancelada`));
        return;
      }
      options.signal?.addEventListener('abort', onAbort, { once: true });
      if (options.timeoutMs !== undefined) {
        timer = setTimeout(() => {
          this.pending.delete(id);
          cleanup();
          reject(new JsonRpcError(-32001, `tempo esgotado esperando ${method}`));
        }, options.timeoutMs);
      }
      this.pending.set(id, {
        method,
        resolve: (value) => resolve(value as T),
        reject,
        cleanup,
      });
      this.send(params === undefined ? { jsonrpc: '2.0', id, method } : { jsonrpc: '2.0', id, method, params });
    });
  }

  notify(method: string, params?: unknown): void {
    if (this.closedError) {
      return;
    }
    this.send(params === undefined ? { jsonrpc: '2.0', method } : { jsonrpc: '2.0', method, params });
  }

  close(error: Error = new ConnectionClosedError()): void {
    if (this.closedError) {
      return;
    }
    this.closedError = error;
    for (const [id, pending] of this.pending) {
      this.pending.delete(id);
      pending.cleanup();
      pending.reject(error);
    }
    for (const listener of this.closeListeners) {
      listener(error);
    }
  }

  private send(message: JsonRpcMessage): void {
    if (this.closedError) {
      return;
    }
    try {
      this.write(encodeMessage(message));
    } catch (error) {
      this.close(new ConnectionClosedError(`falha ao escrever para o motor: ${(error as Error).message}`));
    }
  }

  private handleLine(line: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      // O motor só deve escrever JSON no stdout; qualquer outra coisa é ruído (log).
      this.log(`linha ignorada (não é JSON): ${line.slice(0, 200)}`);
      return;
    }
    const classified = classifyMessage(parsed);
    if (!classified) {
      this.log(`mensagem ignorada (não é JSON-RPC 2.0): ${line.slice(0, 200)}`);
      return;
    }
    switch (classified.kind) {
      case 'response':
        this.handleResponse(classified.message);
        break;
      case 'notification':
        this.dispatchNotification(classified.message);
        break;
      case 'request':
        void this.dispatchRequest(classified.message);
        break;
    }
  }

  private handleResponse(message: JsonRpcResponse): void {
    if (message.id === null) {
      this.log(`erro sem id recebido: ${JSON.stringify(message.error)}`);
      return;
    }
    const pending = this.pending.get(message.id);
    if (!pending) {
      this.log(`resposta para id desconhecido: ${String(message.id)}`);
      return;
    }
    this.pending.delete(message.id);
    pending.cleanup();
    if (message.error) {
      pending.reject(new JsonRpcError(message.error.code, message.error.message, message.error.data));
    } else {
      pending.resolve(message.result ?? null);
    }
  }

  private dispatchNotification(message: JsonRpcNotification): void {
    try {
      this.notificationHandler?.(message.method, message.params);
    } catch (error) {
      this.log(`erro tratando notificação ${message.method}: ${(error as Error).message}`);
    }
  }

  private async dispatchRequest(message: JsonRpcRequest): Promise<void> {
    if (!this.requestHandler) {
      this.send({
        jsonrpc: '2.0',
        id: message.id,
        error: { code: ErrorCodes.methodNotFound, message: `método não suportado: ${message.method}` },
      });
      return;
    }
    try {
      const result = await this.requestHandler(message.method, message.params);
      this.send({ jsonrpc: '2.0', id: message.id, result: result ?? null });
    } catch (error) {
      const rpcError =
        error instanceof JsonRpcError
          ? error
          : new JsonRpcError(ErrorCodes.internalError, (error as Error)?.message ?? String(error));
      this.send({ jsonrpc: '2.0', id: message.id, error: rpcError.toObject() });
    }
  }
}
