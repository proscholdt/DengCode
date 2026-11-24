/**
 * Localiza o executável do motor, sem efeitos colaterais (dependências injetadas).
 *
 * Ordem: configuração `dengcode.caminhoMotor` → `dengcode` no PATH →
 * `uv run dengcode` quando uma pasta do workspace é o próprio repositório do
 * DengCode → binário já baixado no storage da extensão.
 */

import * as path from 'node:path';
import { assetFileName } from './release';

export type EngineSource = 'setting' | 'path' | 'uv' | 'download';

export interface EngineCommand {
  /** Executável a chamar. */
  command: string;
  /** Argumentos que vêm antes do subcomando (`acp`, `--version`). */
  args: string[];
  source: EngineSource;
  /** `.cmd`/`.bat` no Windows só rodam via shell. */
  shell: boolean;
  /** Variáveis extras para o processo. */
  env: Record<string, string>;
  /** Texto para mostrar ao usuário. */
  description: string;
}

export interface LocatorDeps {
  platform: string;
  arch: string;
  /** Conteúdo de PATH (ou Path no Windows). */
  pathEnv: string;
  /** Conteúdo de PATHEXT (Windows). */
  pathExt?: string;
  isFile(filePath: string): Promise<boolean>;
  workspaceFolders: string[];
  /** Pasta onde a extensão guarda binários baixados (`globalStorageUri`). */
  storageDir: string;
  extensionVersion: string;
  /** Runtime Node da extensão (para motores `.js`, útil em desenvolvimento e testes). */
  nodeExecPath: string;
}

export type LocateResult =
  | { found: true; engine: EngineCommand }
  | { found: false; reason: string; configuredPathMissing?: string };

function pathApi(platform: string): typeof path.posix {
  return platform === 'win32' ? path.win32 : path.posix;
}

/** Monta o comando para um caminho de executável explícito. */
export function commandForPath(filePath: string, source: EngineSource, deps: Pick<LocatorDeps, 'nodeExecPath' | 'platform'>): EngineCommand {
  const ext = pathApi(deps.platform).extname(filePath).toLowerCase();
  if (ext === '.js' || ext === '.mjs' || ext === '.cjs') {
    return {
      command: deps.nodeExecPath,
      args: [filePath],
      source,
      shell: false,
      // Dentro do VS Code, o execPath é o Electron: esta variável o faz agir como Node.
      env: { ELECTRON_RUN_AS_NODE: '1' },
      description: `${filePath} (script Node)`,
    };
  }
  const shell = deps.platform === 'win32' && (ext === '.cmd' || ext === '.bat');
  return { command: filePath, args: [], source, shell, env: {}, description: filePath };
}

/** Procura um executável no PATH, respeitando PATHEXT no Windows. */
export async function findOnPath(name: string, deps: Pick<LocatorDeps, 'platform' | 'pathEnv' | 'pathExt' | 'isFile'>): Promise<string | undefined> {
  const api = pathApi(deps.platform);
  const isWindows = deps.platform === 'win32';
  const dirs = deps.pathEnv
    .split(isWindows ? ';' : ':')
    .map((d) => d.trim().replace(/^"(.*)"$/, '$1'))
    .filter((d) => d.length > 0);
  const extensions = isWindows
    ? (deps.pathExt ?? '.EXE;.CMD;.BAT;.COM')
        .split(';')
        .map((e) => e.trim().toLowerCase())
        .filter((e) => e.length > 0)
    : [''];
  for (const dir of dirs) {
    for (const ext of extensions) {
      const candidate = api.join(dir, name + ext);
      if (await deps.isFile(candidate)) {
        return candidate;
      }
    }
  }
  return undefined;
}

/** Uma pasta é o repositório do DengCode se tem o pacote da CLI. */
export function dengcodeRepoMarker(folder: string, platform: string): string {
  return pathApi(platform).join(folder, 'packages', 'dengcode-cli', 'pyproject.toml');
}

/** Caminho do binário baixado para a versão da extensão. */
export function downloadedEnginePath(deps: Pick<LocatorDeps, 'storageDir' | 'extensionVersion' | 'platform' | 'arch'>): string | undefined {
  const fileName = assetFileName(deps.platform, deps.arch);
  if (!fileName) {
    return undefined;
  }
  return pathApi(deps.platform).join(deps.storageDir, 'engine', `v${deps.extensionVersion}`, fileName);
}

export async function locateEngine(configuredPath: string, deps: LocatorDeps): Promise<LocateResult> {
  const configured = configuredPath.trim();
  if (configured.length > 0) {
    if (await deps.isFile(configured)) {
      return { found: true, engine: commandForPath(configured, 'setting', deps) };
    }
    return {
      found: false,
      reason: `o caminho configurado em dengcode.caminhoMotor não existe: ${configured}`,
      configuredPathMissing: configured,
    };
  }

  const onPath = await findOnPath('dengcode', deps);
  if (onPath) {
    return { found: true, engine: commandForPath(onPath, 'path', deps) };
  }

  for (const folder of deps.workspaceFolders) {
    if (await deps.isFile(dengcodeRepoMarker(folder, deps.platform))) {
      const uv = await findOnPath('uv', deps);
      if (uv) {
        const base = commandForPath(uv, 'uv', deps);
        return {
          found: true,
          engine: {
            ...base,
            args: [...base.args, 'run', '--project', folder, 'dengcode'],
            description: `uv run --project ${folder} dengcode`,
          },
        };
      }
    }
  }

  const downloaded = downloadedEnginePath(deps);
  if (downloaded && (await deps.isFile(downloaded))) {
    return { found: true, engine: commandForPath(downloaded, 'download', deps) };
  }

  return { found: false, reason: 'o executável dengcode não foi encontrado (configuração, PATH, uv ou download)' };
}
