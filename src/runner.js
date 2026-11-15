import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { pythonPath, skillDir, resolveScript, dataDir } from './paths.js';

const isWin = process.platform === 'win32';

/**
 * 记一次工具调用的墙钟耗时到 <data>/runs.jsonl。
 *
 * 为什么要在壳侧记：翻译真正慢的是模型读素材、写译文那一段，Python 进程只负责
 * 输出待译项和写回结果，两次调用之间是空的。只看 Python 侧的 timing 会得出
 * 「翻译只花几百毫秒」这种结论。这里记的是「从发起到返回」的真实等待，
 * 相邻两次 fill 的 ts 之差就是模型侧花的时间。
 */
function logRun(entry) {
  try {
    const p = path.join(dataDir(), 'runs.jsonl');
    try {
      if (fs.statSync(p).size > 2 * 1024 * 1024) {
        fs.renameSync(p, path.join(dataDir(), 'runs.1.jsonl'));
      }
    } catch {
      /* 文件还不存在 */
    }
    // v=记录格式版本。v>=2 时 flags/path 必然存在（可能是空数组/空串）；
    // 没有 v 的是早期记录，两个字段根本没写，读的时候要当「未知」而不是「空」。
    fs.appendFileSync(p, JSON.stringify({ v: 2, ts: new Date().toISOString(), ...entry }) + '\n', 'utf8');
  } catch {
    /* 记日志失败不能影响主流程 */
  }
}

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
export function runPy(scriptName, args, { timeoutMs = 600000, signal, env } = {}) {
  const py = pythonPath();
  const file = resolveScript(scriptName);
  const t0 = Date.now();
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
          // 壳侧算好的产物目录：Python 不读 DSH 配置，只认这个变量。
          ...(env || {}),
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
      logRun({
        script: scriptName,
        sub: (args || []).find((a) => typeof a === 'string' && !a.startsWith('-')) || '',
        // 只记 sub 分不出「--timing」和「--timing --last」；参数留一份原样
        flags: (args || []).filter((a) => typeof a === 'string' && a.startsWith('-')),
        // 第一个带路径分隔符的参数：事后能认出这是哪篇（prepare 时是 pdf，其余是工作目录）
        path: (args || []).find((a) => typeof a === 'string' && /[\\/]/.test(a)) || '',
        code: result.code,
        ms: Date.now() - t0,
        timeout: Boolean(result.timedOut),
        aborted: Boolean(result.aborted),
      });
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
