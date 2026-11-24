/**
 * Gerência do motor no VS Code: localizar, baixar e checar a versão.
 */

import * as fs from 'node:fs';
import * as vscode from 'vscode';
import { readConfig } from '../config';
import { DownloadNotFoundError, downloadFile } from './download';
import type { Logger } from './engineProcess';
import { runVersion } from './engineProcess';
import { downloadedEnginePath, locateEngine, type EngineCommand, type LocatorDeps } from './locator';
import { releaseAsset } from './release';
import { checkCompatibility, describeCompatibility } from './version';

const INSTALL_DOCS = 'https://github.com/proscholdt/DengCode#instala%C3%A7%C3%A3o';

async function isFile(filePath: string): Promise<boolean> {
  try {
    return (await fs.promises.stat(filePath)).isFile();
  } catch {
    return false;
  }
}

export class EngineManager {
  private verified = new Map<string, string>();

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly logger: Logger,
  ) {}

  get extensionVersion(): string {
    return String((this.context.extension.packageJSON as { version?: string }).version ?? '0.0.0');
  }

  locatorDeps(): LocatorDeps {
    return {
      platform: process.platform,
      arch: process.arch,
      pathEnv: process.env.PATH ?? process.env.Path ?? '',
      pathExt: process.env.PATHEXT,
      isFile,
      workspaceFolders: (vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath),
      storageDir: this.context.globalStorageUri.fsPath,
      extensionVersion: this.extensionVersion,
      nodeExecPath: process.execPath,
    };
  }

  /**
   * Devolve o comando do motor, já com a versão checada.
   * Sem motor, mostra as opções ao usuário e devolve `undefined`.
   */
  async resolve(): Promise<EngineCommand | undefined> {
    const config = readConfig();
    const result = await locateEngine(config.enginePath, this.locatorDeps());
    if (!result.found) {
      this.logger.info(`motor não encontrado: ${result.reason}`);
      const engine = await this.offerInstall(result.reason, Boolean(result.configuredPathMissing));
      return engine ? this.checkVersion(engine) : undefined;
    }
    this.logger.info(`motor encontrado (${result.engine.source}): ${result.engine.description}`);
    return this.checkVersion(result.engine);
  }

  /** Compara `major.minor` do motor com a extensão; avisa, mas não bloqueia. */
  async checkVersion(engine: EngineCommand): Promise<EngineCommand | undefined> {
    const key = `${engine.command} ${engine.args.join(' ')}`;
    if (this.verified.has(key)) {
      return engine;
    }
    let output: string;
    try {
      output = await runVersion(engine);
    } catch (error) {
      const message = `Não foi possível executar o motor do DengCode: ${(error as Error).message}`;
      this.logger.error(message);
      void vscode.window.showErrorMessage(message, 'Abrir configurações').then((choice) => {
        if (choice) {
          void vscode.commands.executeCommand('workbench.action.openSettings', 'dengcode.caminhoMotor');
        }
      });
      return undefined;
    }
    const compat = checkCompatibility(output, this.extensionVersion);
    const description = describeCompatibility(compat, this.extensionVersion);
    this.logger.info(description);
    if (!compat.compatible) {
      void vscode.window
        .showWarningMessage(`DengCode: ${description}.`, 'Baixar versão compatível', 'Usar mesmo assim')
        .then(async (choice) => {
          if (choice === 'Baixar versão compatível') {
            await this.download();
          }
        });
    }
    this.verified.set(key, output.trim());
    return engine;
  }

  versionOf(engine: EngineCommand): string | undefined {
    return this.verified.get(`${engine.command} ${engine.args.join(' ')}`);
  }

  private async offerInstall(reason: string, configuredMissing: boolean): Promise<EngineCommand | undefined> {
    const download = 'Baixar motor';
    const configure = 'Configurar caminho';
    const docs = 'Como instalar';
    const message = configuredMissing
      ? `DengCode: ${reason}.`
      : 'DengCode: o motor (dengcode) não foi encontrado no PATH. Baixe o binário, configure o caminho ou instale com "uv tool install dengcode".';
    const choice = await vscode.window.showErrorMessage(message, download, configure, docs);
    if (choice === download) {
      return this.download();
    }
    if (choice === configure) {
      await vscode.commands.executeCommand('workbench.action.openSettings', 'dengcode.caminhoMotor');
    } else if (choice === docs) {
      await vscode.env.openExternal(vscode.Uri.parse(INSTALL_DOCS));
    }
    return undefined;
  }

  /** Baixa o binário da release que corresponde à versão da extensão. */
  async download(): Promise<EngineCommand | undefined> {
    const deps = this.locatorDeps();
    const asset = releaseAsset(this.extensionVersion, deps.platform, deps.arch);
    const destination = downloadedEnginePath(deps);
    if (!asset || !destination) {
      void vscode.window.showErrorMessage(
        `DengCode: não há binário publicado para ${deps.platform}-${deps.arch}. Instale o motor com "uv tool install dengcode" e configure dengcode.caminhoMotor.`,
      );
      return undefined;
    }
    try {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `DengCode: baixando motor v${this.extensionVersion} (${asset.target})`, cancellable: true },
        async (progress, token) => {
          const controller = new AbortController();
          token.onCancellationRequested(() => controller.abort());
          let lastPct = 0;
          await downloadFile(asset.url, destination, {
            signal: controller.signal,
            onProgress: (received, total) => {
              if (total) {
                const pct = Math.floor((received / total) * 100);
                if (pct > lastPct) {
                  progress.report({ increment: pct - lastPct, message: `${pct}%` });
                  lastPct = pct;
                }
              }
            },
          });
        },
      );
    } catch (error) {
      const message =
        error instanceof DownloadNotFoundError
          ? `DengCode: a release v${this.extensionVersion} ainda não tem o binário ${asset.fileName}. Instale com "uv tool install dengcode" ou configure dengcode.caminhoMotor.`
          : `DengCode: falha ao baixar o motor: ${(error as Error).message}`;
      this.logger.error(message);
      void vscode.window.showErrorMessage(message);
      return undefined;
    }
    this.logger.info(`motor baixado em ${destination}`);
    const result = await locateEngine('', { ...deps, pathEnv: '' });
    return result.found ? result.engine : undefined;
  }
}
