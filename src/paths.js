import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const has = (file) => {
  try {
    return fs.existsSync(file);
  } catch {
    return false;
  }
};

// 设置界面（宿主 Config）与前端面板的实时值：每次现读，改完不必重启。
let liveSource = null;

export function setConfigSource(fn) {
  liveSource = typeof fn === 'function' ? fn : null;
}

// config.json 是文件兜底：没装设置界面、或用命令行直接跑时仍能配置。
function fileConfig() {
  try {
    return JSON.parse(fs.readFileSync(path.join(root, 'config.json'), 'utf8'));
  } catch {
    return {};
  }
}

// 设置页未填的字段是空字符串，不能让它盖掉 config.json 里的真实配置。
export function config() {
  const merged = { ...fileConfig() };
  let live = {};
  try {
    const value = liveSource?.();
    if (value && typeof value === 'object') live = value;
  } catch {
    live = {};
  }
  for (const [key, value] of Object.entries(live)) {
    if (value !== '' && value !== null && value !== undefined) merged[key] = value;
  }
  return merged;
}

// 产物目录：环境变量 > 面板保存值 > 设置页 Config / config.json > <工作根>/translate_result。
// 目录不存在就建；建不出来直接抛错，不静默落回旧位置——否则用户会以为设置生效了。
export function outputDir() {
  const saved = readState().outputDir;
  const explicit = process.env.DSH_PAPER_OUTPUT || saved || config().outputDir;
  const dir = explicit ? path.resolve(String(explicit)) : path.join(projectRoot(), 'translate_result');
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

// 用户数据根：论文工作目录与默认输出都在它下面。
// 没有显式配置时退到插件所在目录的旁边（项目文件夹内），不写系统盘的用户目录。
export function projectRoot() {
  const explicit = process.env.DSH_PAPER_PROJECT || config().projectRoot;
  if (explicit) return path.resolve(explicit);
  return path.join(path.dirname(root), 'paper-translate-work');
}

// Python 脚本目录：插件自带副本优先，保证行为可复现；
// 想跟外部 skill 目录联动时，用 config.skillDir 或 DSH_PAPER_SKILL 指定。
export function skillDir() {
  const explicit = process.env.DSH_PAPER_SKILL || config().skillDir;
  if (explicit) return path.resolve(explicit);
  const bundled = path.join(root, 'python');
  if (has(path.join(bundled, 'run.py'))) return bundled;
  return path.join(projectRoot(), '.workbuddy', 'skills', 'paper-translate');
}

// 兼容两种布局：插件内脚本平铺在 python/，skill 里在 scripts/。
export function resolveScript(name) {
  const flat = path.join(skillDir(), name);
  if (has(flat)) return flat;
  return path.join(skillDir(), 'scripts', name);
}

export function dataDir() {
  const explicit = process.env.DSH_PAPER_DATA || config().dataDir;
  const dir = explicit ? path.resolve(explicit) : path.join(root, 'data');
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

export function venvDir() {
  return path.join(root, '.venv');
}

const venvLaunchers = () =>
  process.platform === 'win32'
    ? [['Scripts', 'python.exe'], ['Scripts', 'python3.exe']]
    : [['bin', 'python'], ['bin', 'python3']];

// 优先级：环境变量 > config.json > 插件自带 venv > 项目 venv > PATH。
export function pythonPath() {
  const fromEnv = process.env.DSH_PAPER_PYTHON;
  if (fromEnv) return fromEnv;
  const explicit = config().pythonPath;
  if (explicit) return explicit;
  const bases = [
    venvDir(),
    path.join(projectRoot(), '.workbuddy', '.venv', 'paper-translate'),
    path.join(skillDir(), '.venv'),
  ];
  for (const base of bases) {
    for (const parts of venvLaunchers()) {
      const candidate = path.join(base, ...parts);
      if (has(candidate)) return candidate;
    }
  }
  return process.platform === 'win32' ? 'python' : 'python3';
}

export function statePath() {
  return path.join(dataDir(), 'state.json');
}

export function readState() {
  try {
    return JSON.parse(fs.readFileSync(statePath(), 'utf8'));
  } catch {
    return {};
  }
}

export function writeState(patch) {
  const next = { ...readState(), ...patch };
  fs.writeFileSync(statePath(), JSON.stringify(next, null, 2), 'utf8');
  return next;
}

// 论文工作目录：落在 <projectRoot>/build/papers/<slug>，同一篇重复准备会复用。
export function workDirFor(pdf) {
  const base = path.basename(pdf, path.extname(pdf));
  const slug = base.replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 80) || 'paper';
  return path.join(projectRoot(), 'build', 'papers', slug);
}
