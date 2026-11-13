import { spawn } from 'node:child_process';
import { pythonPath, skillDir, resolveScript } from './paths.js';

const isWin = process.platform === 'win32';

function killTree(child) {
  if (!child || child.killed || child.exitCode !== null) return;
  if (isWin && child.pid) {
    try {
      spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      return;
    } catch {
      /* 落到 child.kill */
    }
  }
  try {
    child.kill('SIGKILL');
  } catch {
    /* 已退出 */
  }
}

/**
 * 跑 skill 里的一个 Python 脚本，收 stdout/stderr。
 * 不解析业务语义，只保证：编码正确、超时可中断、不弹黑窗。
 */
export function runPy(scriptName, args, { timeoutMs = 600000, signal } = {}) {
  const py = pythonPath();
  const file = resolveScript(scriptName);
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(py, ['-u', file, ...args], {
        cwd: skillDir(),
        windowsHide: true,
        shell: false,
        env: {
          ...process.env,
          // Windows 下 Python 默认按 GBK 输出，run.py 用 utf-8 解码子脚本，不一致就乱码。
          PYTHONIOENCODING: 'utf-8',
          PYTHONUTF8: '1',
          PYTHONDONTWRITEBYTECODE: '1',
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (error) {
      resolve({ code: -1, stdout: '', stderr: `无法启动 Python（${py}）：${error.message}`, timedOut: false });
      return;
    }

    let stdout = '';
    let stderr = '';
    let settled = false;
    let timer;

    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
      resolve(result);
    };

    const onAbort = () => {
      killTree(child);
      finish({ code: -3, stdout, stderr: `${stderr}\n已取消。`, timedOut: false, aborted: true });
    };

    timer = setTimeout(() => {
      killTree(child);
      finish({ code: -9, stdout, stderr: `${stderr}\n执行超过 ${Math.round(timeoutMs / 1000)} 秒，已终止。`, timedOut: true });
    }, timeoutMs);

    if (signal) {
      if (signal.aborted) {
        onAbort();
        return;
      }
      signal.addEventListener('abort', onAbort, { once: true });
    }

    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      stdout += chunk;
      // 防止模型把一整篇 PDF 的原文全塞回上下文
      if (stdout.length > 4_000_000) {
        killTree(child);
        finish({ code: -2, stdout: stdout.slice(0, 4_000_000), stderr: 'Python 输出过大，已截断并终止。', timedOut: false });
      }
    });
    child.stderr.on('data', (chunk) => {
      stderr = (stderr + chunk).slice(-8000);
    });
    child.on('error', (error) => {
      finish({ code: -1, stdout, stderr: `${stderr}\n无法启动 Python（${py}）：${error.message}`, timedOut: false });
    });
    child.on('close', (code) => {
      finish({ code: code ?? -1, stdout, stderr, timedOut: false });
    });
  });
}

export function pythonHint() {
  return `Python: ${pythonPath()}\nSkill:  ${skillDir()}`;
}
