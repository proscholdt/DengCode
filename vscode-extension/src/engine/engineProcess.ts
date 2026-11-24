/**
 * Processo do motor (`dengcode acp`) ligado a uma conexão JSON-RPC.
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { ConnectionClosedError, JsonRpcConnection } from '../protocol/jsonrpc';
import type { EngineCommand } from './locator';
import { buildSpawnSpec } from './spawnSpec';

export interface Logger {
  info(message: string): void;
  error(message: string): void;
}

export class EngineProcess {
  readonly connection: JsonRpcConnection;
  private readonly child: ChildProcessWithoutNullStreams;
  private exited = false;
  private readonly exitListeners: Array<(code: number | null, signal: NodeJS.Signals | null) => void> = [];

  private constructor(child: ChildProcessWithoutNullStreams, logger: Logger) {
    this.child = child;
    this.connection = new JsonRpcConnection(
      (line) => {
        child.stdin.write(line);
      },
      (message) => logger.info(`[acp] ${message}`),
    );
    child.stdout.on('data', (chunk: Buffer) => this.connection.receive(chunk));
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (text: string) => {
      for (const line of text.split(/\r?\n/)) {
        if (line.trim()) {
          logger.info(`[motor] ${line}`);
        }
      }
    });
    child.stdin.on('error', (error) => logger.error(`stdin do motor: ${error.message}`));
    child.on('error', (error) => {
      logger.error(`falha ao iniciar o motor: ${error.message}`);
      this.connection.close(new ConnectionClosedError(`falha ao iniciar o motor: ${error.message}`));
    });
    child.on('exit', (code, signal) => {
      this.exited = true;
      logger.info(`motor encerrou (código ${String(code)}${signal ? `, sinal ${signal}` : ''})`);
      this.connection.end(new ConnectionClosedError(`o motor encerrou (código ${String(code)})`));
      for (const listener of this.exitListeners) {
        listener(code, signal);
      }
    });
  }

  static start(
    engine: EngineCommand,
    options: { cwd: string; env: Record<string, string>; logger: Logger },
  ): EngineProcess {
    const spec = buildSpawnSpec(engine, ['acp'], process.env, options.env);
    options.logger.info(`iniciando motor: ${engine.description} acp (cwd: ${options.cwd})`);
    const child = spawn(spec.command, spec.args, {
      cwd: options.cwd,
      env: spec.env,
      shell: spec.shell,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
    return new EngineProcess(child, options.logger);
  }

  get pid(): number | undefined {
    return this.child.pid;
  }

  get hasExited(): boolean {
    return this.exited;
  }

  onExit(listener: (code: number | null, signal: NodeJS.Signals | null) => void): void {
    this.exitListeners.push(listener);
  }

  /** Fecha o stdin (o motor deve sair sozinho) e, se não sair, mata o processo. */
  async stop(graceMs = 2000): Promise<void> {
    if (this.exited) {
      return;
    }
    this.connection.close();
    this.child.stdin.end();
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        if (!this.exited) {
          this.child.kill();
        }
        resolve();
      }, graceMs);
      this.child.once('exit', () => {
        clearTimeout(timer);
        resolve();
      });
    });
  }
}

/** Roda `<motor> --version` e devolve a saída (stdout + stderr). */
export function runVersion(engine: EngineCommand, timeoutMs = 20_000): Promise<string> {
  const spec = buildSpawnSpec(engine, ['--version'], process.env);
  return new Promise((resolve, reject) => {
    const child = spawn(spec.command, spec.args, { env: spec.env, shell: spec.shell, windowsHide: true });
    let output = '';
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`"${engine.description} --version" não respondeu em ${timeoutMs / 1000}s`));
    }, timeoutMs);
    child.stdout.on('data', (d: Buffer) => (output += d.toString('utf8')));
    child.stderr.on('data', (d: Buffer) => (output += d.toString('utf8')));
    child.on('error', (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on('exit', (code) => {
      clearTimeout(timer);
      if (code === 0) {
        resolve(output);
      } else {
        reject(new Error(`"${engine.description} --version" saiu com código ${String(code)}: ${output.trim().slice(0, 300)}`));
      }
    });
  });
}
