import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import {
  commandForPath,
  dengcodeRepoMarker,
  downloadedEnginePath,
  findOnPath,
  locateEngine,
  type LocatorDeps,
} from '../../src/engine/locator';
import { assetFileName, platformTarget, releaseAsset } from '../../src/engine/release';
import { buildSpawnSpec, quoteForCmd, settingsEnv } from '../../src/engine/spawnSpec';
import { checkCompatibility, describeCompatibility, parseVersion } from '../../src/engine/version';

describe('seleção do binário da release', () => {
  it('mapeia SO/arquitetura para os alvos da fase 8', () => {
    assert.equal(platformTarget('win32', 'x64'), 'win-x64');
    assert.equal(platformTarget('linux', 'x64'), 'linux-x64');
    assert.equal(platformTarget('linux', 'arm64'), 'linux-arm64');
    assert.equal(platformTarget('darwin', 'x64'), 'macos-x64');
    assert.equal(platformTarget('darwin', 'arm64'), 'macos-arm64');
  });

  it('plataformas sem binário devolvem undefined', () => {
    assert.equal(platformTarget('win32', 'arm64'), undefined);
    assert.equal(platformTarget('linux', 'ia32'), undefined);
    assert.equal(platformTarget('freebsd', 'x64'), undefined);
    assert.equal(releaseAsset('0.1.0', 'aix', 'ppc64'), undefined);
  });

  it('monta o nome do arquivo e a URL', () => {
    assert.equal(assetFileName('win32', 'x64'), 'dengcode-win-x64.exe');
    assert.equal(assetFileName('darwin', 'arm64'), 'dengcode-macos-arm64');
    assert.deepEqual(releaseAsset('0.1.0', 'linux', 'x64'), {
      target: 'linux-x64',
      fileName: 'dengcode-linux-x64',
      url: 'https://github.com/proscholdt/DengCode/releases/download/v0.1.0/dengcode-linux-x64',
    });
    assert.equal(
      releaseAsset('v1.2.3', 'win32', 'x64')?.url,
      'https://github.com/proscholdt/DengCode/releases/download/v1.2.3/dengcode-win-x64.exe',
    );
  });
});

describe('versão do motor', () => {
  it('lê a versão da saída de --version', () => {
    assert.deepEqual(parseVersion('dengcode 0.1.0\n'), { major: 0, minor: 1, patch: 0 });
    assert.deepEqual(parseVersion('dengcode 1.2.3-rc.1'), { major: 1, minor: 2, patch: 3, prerelease: 'rc.1' });
    assert.equal(parseVersion('sem versão'), undefined);
  });

  it('compatível quando major.minor coincidem', () => {
    assert.equal(checkCompatibility('dengcode 0.1.7', '0.1.0').compatible, true);
    const mismatch = checkCompatibility('dengcode 0.2.0', '0.1.0');
    assert.equal(mismatch.compatible, false);
    assert.match(describeCompatibility(mismatch, '0.1.0'), /incompatível.*0\.1\.x/);
    const garbage = checkCompatibility('Traceback (most recent call last)', '0.1.0');
    assert.equal(garbage.compatible === false && garbage.reason, 'unparseable');
  });
});

function deps(files: string[], overrides: Partial<LocatorDeps> = {}): LocatorDeps {
  const set = new Set(files.map((f) => f.toLowerCase()));
  return {
    platform: 'linux',
    arch: 'x64',
    pathEnv: '/usr/bin:/home/u/.local/bin',
    isFile: async (p) => set.has(p.toLowerCase()),
    workspaceFolders: [],
    storageDir: '/storage',
    extensionVersion: '0.1.0',
    nodeExecPath: '/opt/code/code',
    ...overrides,
  };
}

describe('localização do motor', () => {
  it('a configuração tem prioridade', async () => {
    const r = await locateEngine('/opt/dengcode', deps(['/opt/dengcode', '/usr/bin/dengcode']));
    assert.equal(r.found && r.engine.source, 'setting');
    assert.equal(r.found && r.engine.command, '/opt/dengcode');
  });

  it('caminho configurado inexistente não cai para o PATH', async () => {
    const r = await locateEngine('/nao/existe', deps(['/usr/bin/dengcode']));
    assert.equal(r.found, false);
    assert.equal(!r.found && r.configuredPathMissing, '/nao/existe');
  });

  it('procura no PATH', async () => {
    const r = await locateEngine('', deps(['/home/u/.local/bin/dengcode']));
    assert.equal(r.found && r.engine.source, 'path');
    assert.equal(r.found && r.engine.command, '/home/u/.local/bin/dengcode');
  });

  it('no Windows respeita PATHEXT e roda .cmd via shell', async () => {
    const win = deps(['C:\\Users\\u\\.local\\bin\\dengcode.cmd'], {
      platform: 'win32',
      pathEnv: 'C:\\Windows;"C:\\Users\\u\\.local\\bin"',
      pathExt: '.COM;.EXE;.BAT;.CMD',
    });
    assert.equal(await findOnPath('dengcode', win), 'C:\\Users\\u\\.local\\bin\\dengcode.cmd');
    const r = await locateEngine('', win);
    assert.equal(r.found && r.engine.shell, true);
  });

  it('usa "uv run" quando a pasta aberta é o repositório do DengCode', async () => {
    const repo = '/src/DengCode';
    const r = await locateEngine('', deps([dengcodeRepoMarker(repo, 'linux'), '/usr/bin/uv'], { workspaceFolders: [repo] }));
    assert.equal(r.found && r.engine.source, 'uv');
    assert.deepEqual(r.found && r.engine.args, ['run', '--project', repo, 'dengcode']);
  });

  it('por último, o binário baixado para a versão da extensão', async () => {
    const d = deps([]);
    const downloaded = downloadedEnginePath(d) as string;
    assert.equal(downloaded, '/storage/engine/v0.1.0/dengcode-linux-x64');
    const r = await locateEngine('', deps([downloaded]));
    assert.equal(r.found && r.engine.source, 'download');
  });

  it('sem nada, informa o motivo', async () => {
    const r = await locateEngine('', deps([]));
    assert.equal(r.found, false);
    assert.match(!r.found ? r.reason : '', /não foi encontrado/);
  });

  it('scripts .js rodam com o runtime da extensão', () => {
    const cmd = commandForPath('/t/fake.mjs', 'setting', { nodeExecPath: '/code', platform: 'linux' });
    assert.equal(cmd.command, '/code');
    assert.deepEqual(cmd.args, ['/t/fake.mjs']);
    assert.equal(cmd.env.ELECTRON_RUN_AS_NODE, '1');
  });
});

describe('processo do motor', () => {
  it('configurações viram variáveis de ambiente', () => {
    assert.deepEqual(settingsEnv({ stack: ' opensource ', env: 'prod', permissionMode: 'read_only', workspace: '' }), {
      DENGCODE_STACK: 'opensource',
      DENGCODE_ENV: 'prod',
      DENGCODE_MODE: 'read_only',
    });
  });

  it('monta a chamada com subcomando e UTF-8 forçado', () => {
    const spec = buildSpawnSpec(
      { command: '/bin/dengcode', args: [], source: 'path', shell: false, env: {}, description: '' },
      ['acp'],
      { PATH: '/bin' },
      { DENGCODE_ENV: 'dev' },
    );
    assert.equal(spec.command, '/bin/dengcode');
    assert.deepEqual(spec.args, ['acp']);
    assert.equal(spec.env.PYTHONIOENCODING, 'utf-8');
    assert.equal(spec.env.DENGCODE_ENV, 'dev');
    assert.equal(spec.env.PATH, '/bin');
  });

  it('.cmd no Windows vira linha de comando com aspas', () => {
    const spec = buildSpawnSpec(
      { command: 'C:\\Program Files\\dc\\dengcode.cmd', args: [], source: 'path', shell: true, env: {}, description: '' },
      ['acp'],
      {},
    );
    assert.equal(spec.shell, true);
    assert.equal(spec.command, '"C:\\Program Files\\dc\\dengcode.cmd" acp');
    assert.equal(quoteForCmd('a"b c'), '"a""b c"');
  });
});
