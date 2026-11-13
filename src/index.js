import fs from 'node:fs';
import path from 'node:path';
import { defineTool } from '@deepseek-ai/dsh-tools';
import { runPy, pythonHint } from './runner.js';
import { pythonPath, skillDir, venvDir, projectRoot, workDirFor, readState, writeState, root } from './paths.js';

export const name = 'paper-translate';
export const inject = ['tools'];

const asText = (props = {}) => ({
  schema: { type: 'object', properties: { text: { type: 'string' }, ...props }, additionalProperties: true },
  render: (_args, value) => [{ type: 'text', text: String(value?.text ?? '') }],
});

const str = { type: 'string' };
const int = { type: 'integer' };
const bool = { type: 'boolean' };

// Python 的提示语是给终端用户看的，里面带着命令行。模型看到会去敲 bash，
// 绕过插件后工作目录和编码都容易出错——这里统一抹掉，改由工具描述收口。
const stripCli = (text) =>
  String(text || '')
    .split('\n')
    .filter((line) => !/^\s*python\s+.*\.py/i.test(line))
    .filter((line) => !/^下一步（分批翻译/.test(line.trim()))
    .filter((line) => !/^把上面的每条原文译成中文/.test(line.trim()))
    .join('\n')
    .replace(/交付前建议先跑：\s*\n?/g, '可用 paper_audit 抽检一次。\n')
    .replace(/^需要 PDF 时:.*$/gm, '需要 PDF 时把 pdf 参数设为 true。')
    .replace(/\n{3,}/g, '\n\n')
    .trim();

function resolveWork(input) {
  const value = String(input ?? '').trim();
  if (value) return path.resolve(value);
  const last = readState().lastWork;
  if (last && fs.existsSync(last)) return last;
  throw new Error('还没有工作目录：先调用 paper_prepare 准备一篇 PDF，或显式传 workdir。');
}

function contentOf(work) {
  const file = path.join(work, 'content.json');
  if (!fs.existsSync(file)) throw new Error(`${work} 里没有 content.json，先跑 paper_prepare。`);
  return file;
}

function fail(prefix, result) {
  const body = `${result.stdout || ''}${result.stderr || ''}`.trim();
  return new Error(`${prefix}\n\n${body || '（无输出）'}`);
}

export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: 'paper_doctor',
    description: '检查论文翻译链路是否就绪：Python 解释器、项目内虚拟环境、skill 目录与 PyMuPDF 等依赖。返回路径与缺失项，不翻译、不修改文件。开始翻译前若不确定环境，先调用它。',
    parameters: {},
    output: asText({ python: str, skill: str, ready: bool }),
    async execute() {
      const py = pythonPath();
      const skill = skillDir();
      const lines = [pythonHint(), `工作根目录: ${projectRoot()}`];
      let ready = fs.existsSync(skill);
      lines.push(`脚本目录存在: ${ready ? '是' : '否'}`);
      if (!ready) {
        lines.push('找不到 Python 脚本目录。插件自带 python/ 被子目录覆盖了？');
        lines.push(`可在 ${path.join(root, 'config.json')} 里设置：{"skillDir": "<脚本目录>"}`);
        return { text: lines.join('\n'), python: py, skill, ready: false };
      }
      const r = await runPy('env_check.py', ['--root', root, '--venv', venvDir()], { timeoutMs: 120000 });
      const out = `${r.stdout || ''}${r.stderr || ''}`.trim();
      lines.push('', r.code === 0 ? '依赖检查通过。' : '依赖检查未通过：', out);
      if (r.code !== 0) {
        lines.push('', '缺依赖时装到插件自己的 .venv（不要装进系统 Python）：');
        lines.push(`  "${py}" "${path.join(skill, 'env_check.py')}" --root "${root}" --venv "${venvDir()}" --bootstrap`);
      }
      return { text: lines.join('\n'), python: py, skill, ready: r.code === 0 };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'paper_prepare',
    description: '解析论文 PDF：抽取文本块与版式、定位图区与图形摘要并裁图、重建表格网格，生成 content.json/source.json。这是翻译流程第一步，只需跑一次；重复调用会复用已有工作目录继续。缺依赖时会停下并给出清单——此时先告知用户再决定是否安装，不要自行 pip install。',
    parameters: {
      pdf: { ...str, required: true, description: '本地论文 PDF 的绝对路径' },
      workdir: { ...str, description: '工作目录绝对路径；省略则用 build/papers/<文件名>' },
    },
    output: asText({ work: str, reused: bool }),
    async execute(args, exec) {
      const pdf = path.resolve(String(args.pdf ?? '').trim());
      if (!pdf) throw new Error('pdf 不能为空。');
      if (!fs.existsSync(pdf)) throw new Error(`找不到 PDF：${pdf}`);
      if (!/\.pdf$/i.test(pdf)) throw new Error(`不是 PDF 文件：${pdf}`);
      const work = args.workdir ? path.resolve(args.workdir) : workDirFor(pdf);
      const reused = fs.existsSync(path.join(work, 'content.json'));
      const r = await runPy('run.py', ['prepare', pdf, work], { timeoutMs: 300000, signal: exec?.signal });
      if (r.code === 2) throw fail('缺少 Python 依赖，已停在准备阶段。把下面清单告知用户并等确认，不要自行安装。', r);
      if (r.code !== 0) throw fail(`准备失败（退出码 ${r.code}）。`, r);
      writeState({ lastWork: work, lastPdf: pdf });
      const log = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      return {
        text: [
          reused ? '复用已有工作目录（续译，不会覆盖已填译文）' : '准备完成',
          '',
          log,
          '',
          '下一步：paper_terms 看术语口径 → paper_next 取第一批原文。',
        ].join('\n'),
        work,
        reused,
      };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'paper_terms',
    description: '列出本文命中的术语及其约定译名（按出现频次排序）。翻译前看一次，之后全程保持一致；译文中出现表里的词必须用约定译名。',
    parameters: { workdir: { ...str, description: '工作目录；省略用上一次的目录' } },
    output: asText({ work: str }),
    async execute(args, exec) {
      const work = resolveWork(args.workdir);
      const file = path.join(work, 'structure.json');
      if (!fs.existsSync(file)) throw new Error(`${work} 里没有 structure.json，先跑 paper_prepare。`);
      const r = await runPy('glossary.py', [file], { timeoutMs: 120000, signal: exec?.signal });
      if (r.code !== 0) throw fail('术语表读取失败。', r);
      return { text: stripCli(`${r.stdout || ''}${r.stderr || ''}`), work };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'paper_next',
    description: '取下一批待译原文（默认 20 条，约 9000 字符），每条以单独一行 @@序号 开头。只译返回的内容，不要读 content.json 或 structure.json 取原文。表格已在批里打印结构，照抄行列、只换文字。',
    parameters: {
      workdir: { ...str, description: '工作目录；省略用上一次的目录' },
      n: { ...int, description: '本批条数 1–40，默认 20' },
    },
    output: asText({ work: str, n: int }),
    async execute(args, exec) {
      const work = resolveWork(args.workdir);
      const content = contentOf(work);
      const n = Math.min(Math.max(Number(args.n ?? 20) || 20, 1), 40);
      const r = await runPy('fill.py', [content, '--next', String(n)], { timeoutMs: 180000, signal: exec?.signal });
      if (r.code !== 0) throw fail('取原文失败。', r);
      const body = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      return {
        text: [
          body,
          '',
          '---',
          '提交方式：调用 paper_fill，把译文直接写进 batch 参数（每条一行 @@序号，下一行起写译文），不要写文件、不要在终端跑命令。',
        ].join('\n'),
        work,
        n,
      };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'paper_fill',
    description: '回填译文。batch 直接传文本：每条以单独一行 @@序号 开头，下面写译文，段落之间空行。写入后当场做数字比对与术语口径检查，警告要点名到具体 @@序号，只重译那一条再提交。不要改用文件工具或命令行写批文件。',
    parameters: {
      workdir: { ...str, description: '工作目录；省略用上一次的目录' },
      batch: { ...str, required: true, description: '@@序号 + 译文的纯文本' },
      dry_run: { ...bool, description: '只校验不落盘，用于先看检查结果' },
    },
    output: asText({ work: str, written: int }),
    async execute(args, exec) {
      const work = resolveWork(args.workdir);
      const content = contentOf(work);
      const batch = String(args.batch ?? '').trim();
      if (!batch) throw new Error('batch 为空：先 paper_next 取原文。');
      if (!/^\s*@@\s*\d+/m.test(batch)) {
        throw new Error('batch 里没有找到任何「@@序号」行。格式是每条单独一行 @@12，下一行起写译文。');
      }
      const file = path.join(work, `.dsh-batch-${Date.now()}.txt`);
      fs.writeFileSync(file, `${batch}\n`, 'utf8');
      const argv = [content, '--file', file];
      if (args.dry_run) argv.push('--dry-run');
      const r = await runPy('fill.py', argv, { timeoutMs: 180000, signal: exec?.signal });
      const body = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      const written = Number(/写入\s*(\d+)\s*项/.exec(body)?.[1] ?? 0);
      if (!written && r.code !== 0) throw fail('回填失败。', r);
      return {
        text: [
          args.dry_run ? `校验通过（dry-run，未写入）：${written} 项` : `已回填 ${written} 项`,
          '',
          body,
          '',
          written && !args.dry_run ? '继续：paper_next 取下一批；全部译完再 paper_status 确认。' : '',
        ].join('\n').trim(),
        work,
        written,
      };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'paper_status',
    description: '查看翻译进度：已完成/待处理条数、按章节分布、下一批序号区间。中断后恢复也用它。',
    parameters: { workdir: { ...str, description: '工作目录；省略用上一次的目录' } },
    output: asText({ work: str, done: bool, pending: int }),
    async execute(args, exec) {
      const work = resolveWork(args.workdir);
      const content = contentOf(work);
      const r = await runPy('fill.py', [content, '--status'], { timeoutMs: 120000, signal: exec?.signal });
      const body = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      const pending = /RESULT:\s*PENDING\s+(\d+)/.exec(body);
      const done = /RESULT:\s*DONE/.test(body);
      // fill.py --status 用退出码表达「还有待译项」，不是错误。
      if (!pending && !done && r.code !== 0) throw fail('读取进度失败。', r);
      return {
        text: [body, '', done ? '全部译完，可以 paper_audit 抽检后 paper_finish。' : '继续用 paper_next 取下一批。'].join('\n'),
        work,
        done,
        pending: pending ? Number(pending[1]) : 0,
      };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'paper_audit',
    description: '语义抽检：把原文与译文并排打印（默认 1 条最高风险 + 2 条随机），用于发现机械检查查不出的问题——意思译反、漏句、否定/条件丢失、数字单位错、术语不合口径。跑一次即可，别反复抽。发现问题用 paper_fill 只重译那一条。',
    parameters: {
      workdir: { ...str, description: '工作目录；省略用上一次的目录' },
      n: { ...int, description: '抽检条数，默认 3' },
      seq: { ...str, description: '指定序号（逗号分隔，如 45,88），忽略 n' },
    },
    output: asText({ work: str }),
    async execute(args, exec) {
      const work = resolveWork(args.workdir);
      contentOf(work);
      const seq = String(args.seq ?? '').trim();
      const n = Math.min(Math.max(Number(args.n ?? 3) || 3, 1), 12);
      const argv = seq ? ['audit', work, '--seq', seq] : ['audit', work, String(n)];
      const r = await runPy('run.py', argv, { timeoutMs: 180000, signal: exec?.signal });
      const body = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      if (r.code !== 0) throw fail('抽检失败。', r);
      return { text: body, work };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'paper_finish',
    description: '排版生成中文 DOCX 并自检（未译项、图号对应、数字一致性、图表引用、公式误判、图片与表格数量）。退出码含义：通过 / 有疑点 / 还没译完。pdf=true 时再导出 PDF（需 LibreOffice 或 Word，耗时 20–40 秒）。',
    parameters: {
      workdir: { ...str, description: '工作目录；省略用上一次的目录' },
      out: { ...str, description: '输出 docx 绝对路径，默认 <work>/译文.docx' },
      pdf: { ...bool, description: '同时导出 PDF，默认 false' },
    },
    output: asText({ work: str, docx: str, pdf: str }),
    async execute(args, exec) {
      const work = resolveWork(args.workdir);
      contentOf(work);
      const docx = args.out ? path.resolve(args.out) : path.join(work, '译文.docx');
      const r = await runPy('run.py', ['finish', work, docx], { timeoutMs: 300000, signal: exec?.signal });
      const body = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      if (r.code === 2) {
        return { text: `还没译完，已停下排版（不会输出半成品）。先 paper_status 看剩余，再 paper_next 继续。\n\n${body}`, work, docx, pdf: '' };
      }
      if (r.code !== 0 && r.code !== 1) throw fail(`排版失败（退出码 ${r.code}）。`, r);

      let tail = '';
      let pdfPath = '';
      if (args.pdf) {
        const p = await runPy('run.py', ['pdf', docx], { timeoutMs: 300000, signal: exec?.signal });
        const pbody = `${p.stdout || ''}${p.stderr || ''}`.trim();
        tail = `\n\n[PDF]\n${pbody}`;
        if (p.code === 0) pdfPath = docx.replace(/\.docx$/i, '.pdf');
      }
      const verdict = r.code === 0 ? '自检通过，可以交付。' : '自检有疑点，逐条看上面的提示再决定是否交付。';
      return { text: [verdict, '', body, tail].join('\n').trim(), work, docx, pdf: pdfPath };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'paper_harvest',
    description: '译完后抽取本文反复出现、词表里还没有的术语候选（优先「缩写 + 全称」对），用于沉淀到 references/terms.md。只列候选，不自动写入。',
    parameters: {
      workdir: { ...str, description: '工作目录；省略用上一次的目录' },
      top: { ...int, description: '候选数量，默认 15' },
    },
    output: asText({ work: str }),
    async execute(args, exec) {
      const work = resolveWork(args.workdir);
      const n = Math.min(Math.max(Number(args.top ?? 15) || 15, 5), 40);
      const r = await runPy('run.py', ['harvest', work, '--top', String(n)], { timeoutMs: 120000, signal: exec?.signal });
      const body = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      if (r.code !== 0) throw fail('术语抽取失败。', r);
      return { text: body, work };
    },
  }));

  // ---- 文献总结：默认不跑，只有用户提到「总结/概括」才触发 ----
  ctx.tools.register(defineTool({
    name: 'paper_summary_brief',
    description: '【默认不做总结，别主动调用】仅当用户明确要求「总结/概括/归纳/提炼/综述/summary」这篇（或这批）文献时才调用。它切出约 8–10k 字符的摘要素材包（摘要全文 + 引言/讨论/结果要点 + 方法小节 + 术语口径），供你写五节中文总结：目的、方法、难点、创新、局限。不读全文、不产生译文、不花排版时间。返回里自带输出格式与硬规则，照着写即可。',
    parameters: {
      workdir: { ...str, description: '工作目录；省略用上一次的目录' },
      intent: { ...str, description: '用户这次的原话。传入后若未出现总结类关键词，直接跳过、不生成素材包' },
      budget: { ...int, description: '素材包字符预算，默认 10000' },
    },
    output: asText({ work: str, hit: bool }),
    async execute(args, exec) {
      const raw = String(args.intent ?? '').trim();
      if (raw) {
        const probe = await runPy('summarize.py', ['--intent', raw], { timeoutMs: 20000, signal: exec?.signal });
        if (!/INTENT:\s*HIT/.test(`${probe.stdout || ''}`)) {
          return {
            text: `用户这句话里没有总结/概括类要求（原话：${raw}），按默认策略跳过，本次不生成总结。如确需总结，让用户明确说一句「总结一下这篇」。`,
            work: '', hit: false,
          };
        }
      }
      const work = resolveWork(args.workdir);
      const n = Math.min(Math.max(Number(args.budget ?? 10000) || 10000, 3000), 20000);
      const r = await runPy('run.py', ['summary', work, '--budget', String(n)], { timeoutMs: 120000, signal: exec?.signal });
      const body = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      if (r.code !== 0) throw fail('素材包生成失败。', r);
      return {
        text: [
          body,
          '',
          '—— 写完后把 JSON 交给 paper_summary_write（workdir 同上，summary 传 JSON 文本）。',
          '多篇一起总结时：每篇各跑一次 brief + write，最后 paper_summary_doc 传所有 workdir。',
        ].join('\n'),
        work, hit: true,
      };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'paper_summary_write',
    description: '提交文献总结的五节内容：解析 JSON、写入 <work>/summary.json，并当场做六项机械校验（节齐全与字数、证据逐字命中原文、数字来自原文、中文纯度、英文标题可溯源、节间重复）。校验不通过会指出哪一节、哪条不达标，改完再提交一次即可。',
    parameters: {
      workdir: { ...str, description: '工作目录；省略用上一次的目录' },
      summary: { ...str, description: '五节总结 JSON 文本（可带 ```json 代码块）' },
    },
    output: asText({ work: str, ok: bool, code: int }),
    async execute(args, exec) {
      const work = resolveWork(args.workdir);
      let text = String(args.summary ?? '').trim();
      const fenced = /```(?:json)?\s*([\s\S]*?)```/i.exec(text);
      if (fenced) text = fenced[1].trim();
      const a = text.indexOf('{');
      const b = text.lastIndexOf('}');
      if (a < 0 || b <= a) throw new Error('没有找到 JSON：请只输出 {"title_en":…,"sections":{…}} 这一份对象。');
      let obj;
      try {
        obj = JSON.parse(text.slice(a, b + 1));
      } catch (e) {
        throw new Error(`JSON 解析失败：${e.message}\n常见原因：字符串里有未转义的换行或引号，请压成一行再提交。`);
      }
      const sec = obj.sections || {};
      const need = ['purpose', 'methods', 'difficulty', 'novelty', 'limitation'];
      const missing = need.filter((k) => !sec[k]);
      if (missing.length) throw new Error(`缺section：${missing.join('、')}（五个都要，没有依据就写「原文未明确说明」）。`);
      fs.writeFileSync(path.join(work, 'summary.json'), JSON.stringify(obj, null, 1), 'utf8');

      const r = await runPy('run.py', ['summary-check', work], { timeoutMs: 60000, signal: exec?.signal });
      const body = `${r.stdout || ''}${r.stderr || ''}`.trim();
      const ok = r.code === 0;
      const verdict = ok ? '校验通过。' : r.code === 1 ? '有疑点：可以交付，但逐条看上面提示，能改就改。' : '必检项未通过，按提示重写后重新提交。';
      return { text: [`${verdict}（已写入 summary.json）`, '', body].join('\n'), work, ok, code: r.code };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'paper_summary_doc',
    description: '把已通过校验的 summary.json 排成【总结】DOCX（单篇=一篇五节；多篇=目录 + 每篇中英文名与五节，自动分页），可选导出 PDF。文件名自动取「译文名/中文标题 [总结]」，也可用 out 指定。',
    parameters: {
      workdirs: { type: 'array', items: { type: 'string' }, description: '一个或多个工作目录（多篇即批量）' },
      out: { ...str, description: '输出 docx 绝对路径；省略则自动命名' },
      pdf: { ...bool, description: '同时导出 PDF，默认 true' },
    },
    output: asText({ docx: str, pdf: str }),
    async execute(args, exec) {
      let dirs = Array.isArray(args.workdirs) ? args.workdirs.map((d) => path.resolve(String(d))) : [];
      if (!dirs.length) dirs = [resolveWork('')];
      const argv = ['summary-doc', ...dirs];
      if (args.out) argv.push('-o', path.resolve(args.out));
      if (args.pdf !== false) argv.push('--pdf');
      const r = await runPy('run.py', argv, { timeoutMs: 420000, signal: exec?.signal });
      const body = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      if (r.code === 2) return { text: `总结还没通过必检项，已停下排版。\n\n${body}`, docx: '', pdf: '' };
      if (r.code !== 0) throw fail('总结排版失败。', r);
      const m = /交付:\s*(.+?)（\d+\s*篇）/.exec(body);
      const docx = m ? m[1].trim() : (args.out ? path.resolve(args.out) : '');
      return { text: body, docx, pdf: docx ? docx.replace(/\.docx$/i, '.pdf') : '' };
    },
  }));
}
