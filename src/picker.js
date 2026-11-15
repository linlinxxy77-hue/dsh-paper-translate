import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const scriptPath = path.resolve(here, '..', 'scripts', 'pick-dir.ps1');

/**
 * 弹 Windows 原生文件夹选择框，返回 { cancelled, path }。
 * 路径以 base64 传给 PowerShell：中文路径直接走命令行参数会被代码页弄坏。
 */
export function pickDirectory(initialPath = '', { timeoutMs = 300000, signal } = {}) {
  if (process.platform !== 'win32') {
    return Promise.reject(new Error('系统文件夹选择框只在 Windows 可用；请直接把绝对路径传给 paper_output 的 path 参数。'));
  }
  const initial = initialPath ? Buffer.from(initialPath, 'utf8').toString('base64') : 'EMPTY';
  const exe = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');

  return new Promise((resolve, reject) => {
    let settled = false;
    const done = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
      fn(value);
    };

    const child = spawn(
      exe,
      ['-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-File', scriptPath, '-InitialPathBase64', initial],
      { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] },
    );

    const onAbort = () => {
      child.kill();
      done(reject, new Error('已取消选择。'));
    };
    const timer = setTimeout(() => {
      child.kill();
      done(reject, new Error('选择框等待超时（5 分钟）；可以直接把路径传给 paper_output 的 path 参数。'));
    }, timeoutMs);

    if (signal?.aborted) {
      onAbort();
      return;
    }
    signal?.addEventListener('abort', onAbort, { once: true });

    let out = '';
    let err = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      out += chunk;
    });
    child.stderr.on('data', (chunk) => {
      err = (err + chunk).slice(-2000);
    });
    child.on('error', (error) => done(reject, new Error(`无法打开选择框：${error.message}`)));
    child.on('close', (code) => {
      const text = out.replace(/^\uFEFF/, '').trim();
      const line = text.split(/\r?\n/).filter(Boolean).pop() || '';
      let parsed;
      try {
        parsed = JSON.parse(line);
      } catch {
        done(reject, new Error(`选择框返回无法解析${err ? `：${err.trim()}` : ''}`));
        return;
      }
      if (parsed?.cancelled === true) {
        done(resolve, { cancelled: true, path: '' });
        return;
      }
      if (parsed?.path && path.isAbsolute(parsed.path)) {
        done(resolve, { cancelled: false, path: parsed.path });
        return;
      }
      done(reject, new Error(parsed?.error ? `选择框出错：${parsed.error}` : `选择框异常退出（code=${code}）。`));
    });
  });
}
