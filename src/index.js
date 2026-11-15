import fs from 'node:fs';
import path from 'node:path';
import Schema from '@deepseek-ai/schemastery';
import { defineTool } from '@deepseek-ai/dsh-tools';
import { runPy, pythonHint } from './runner.js';
import { pickDirectory } from './picker.js';
import {
  pythonPath, skillDir, venvDir, projectRoot, workDirFor, readState, writeState, root,
  config, outputDir, setConfigSource,
} from './paths.js';

export const name = 'paper-translate';
export const inject = ['tools'];

// 宿主读这份声明生成设置页表单，并校验保存的值。
// 三个字段都是「留空即回落」：空串不会覆盖 config.json，优先级见 paths.outputDir()。
export const Config = Schema.object({
  projectRoot: Schema.string().default('')
    .description('工作根目录：中间产物放在 <它>/build/papers/。留空则用 config.json，再退到 ~/paper-translate-work。'),
  outputDir: Schema.string().default('')
    .description('译文与总结的输出目录（绝对路径）。留空则用 <工作根目录>/translate_result，不存在会自动创建。'),
  pythonPath: Schema.string().default('')
    .description('Python 解释器绝对路径。留空则自动探测插件 .venv 与项目内虚拟环境。'),
});

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

// 产物文件名主干：用工作目录名（准备时已按论文名 sanitize），这里再兜一层非法字符。
function outStem(work) {
  const base = path.basename(path.resolve(work)).replace(/[\\/:*?"<>|]+/g, '_').trim();
  return base || 'paper';
}

// 输出目录当前是谁定的——用户改配置后最常问的就是这句。
function outputSource() {
  if (process.env.DSH_PAPER_OUTPUT) return '环境变量 DSH_PAPER_OUTPUT';
  if (readState().outputDir) return 'paper_output 保存的值';
  if (config().outputDir) return '设置页 outputDir / config.json';
  return '默认值 <工作根>/translate_result';
}

function saveOutputDir(dir) {
  const abs = path.resolve(String(dir).trim());
  try {
    fs.mkdirSync(abs, { recursive: true });
  } catch (error) {
    throw new Error(`这个目录建不出来或不可写：${abs}（${error.code || error.message}）。换一个位置，或检查权限。`);
  }
  writeState({ outputDir: abs });
  return abs;
}

function contentOf(work) {
  const file = path.join(work, 'content.json');
  if (!fs.existsSync(file)) {
    if (readState().mode === 'summary') {
      throw new Error(`${work} 是「仅总结」工作目录，本次没有生成待译项。要全文翻译就用 paper_prepare 重新准备一次（不要传 mode=summary）。`);
    }
    throw new Error(`${work} 里没有 content.json，先跑 paper_prepare。`);
  }
  return file;
}

function fail(prefix, result) {
  const body = `${result.stdout || ''}${result.stderr || ''}`.trim();
  return new Error(`${prefix}\n\n${body || '（无输出）'}`);
}

export function apply(ctx, config = {}) {
  // 每次解析路径时现读 ctx.config：用户在设置页保存后，即使插件没重载也能拿到新值。
  setConfigSource(() => {
    try {
      return ctx.config ?? config;
    } catch {
      return config;
    }
  });

  ctx.tools.register(defineTool({
    name: 'paper_doctor',
    description: '检查论文翻译链路是否就绪：Python 解释器、项目内虚拟环境、skill 目录与 PyMuPDF 等依赖。返回路径与缺失项，不翻译、不修改文件。开始翻译前若不确定环境，先调用它。',
    parameters: {},
    output: asText({ python: str, skill: str, ready: bool }),
    async execute() {
      const py = pythonPath();
      const skill = skillDir();
      const lines = [pythonHint(), `工作根目录: ${projectRoot()}`, `输出目录: ${outputDir()}（${outputSource()}）`];
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
    name: 'paper_output',
    description: '查看或修改译文/总结的输出目录。get 返回当前目录与来源；pick 弹出 Windows 原生文件夹选择框，由用户在桌面上点选（会阻塞到用户选完或取消）；set 直接把 path 设为输出目录；reset 清除自定义、回到默认 <工作根>/translate_result。用户说「输出到哪了」「换个输出文件夹」「选个目录存」时用它。改完立即生效，不需要重启；已生成的文件不会自动搬家。',
    parameters: {
      action: { ...str, description: 'get（默认）/ pick / set / reset' },
      path: { ...str, description: 'action=set 时的绝对路径，如 D:/papers/out' },
    },
    output: asText({ dir: str, source: str }),
    async execute(args, exec) {
      const action = String(args.action ?? 'get').trim().toLowerCase();
      if (action === 'get' || !action) {
        const dir = outputDir();
        return { text: `输出目录: ${dir}\n来源: ${outputSource()}`, dir, source: outputSource() };
      }
      if (action === 'reset') {
        writeState({ outputDir: '' });
        const dir = outputDir();
        return { text: `已清除自定义目录，回到默认：${dir}`, dir, source: outputSource() };
      }
      if (action === 'set') {
        const raw = String(args.path ?? '').trim();
        if (!raw) throw new Error('action=set 需要 path 参数（绝对路径）。');
        if (!path.isAbsolute(raw)) throw new Error(`需要绝对路径，收到的是相对路径：${raw}`);
        const dir = saveOutputDir(raw);
        return { text: `输出目录已设为：${dir}\n下次 paper_finish / paper_summary_doc 就落在这里。`, dir, source: outputSource() };
      }
      if (action === 'pick') {
        const current = outputDir();
        let picked;
        try {
          picked = await pickDirectory(current, { signal: exec?.signal });
        } catch (error) {
          throw new Error(`${error.message}\n如果桌面没有出现选择框，可以让用户直接把路径填进设置页的输出目录，或用 paper_output action=set path="D:/xxx"。`);
        }
        if (picked.cancelled) {
          return { text: `已取消，输出目录没变：${current}`, dir: current, source: outputSource() };
        }
        const dir = saveOutputDir(picked.path);
        return { text: `已保存：${dir}\n下次排版就落在这里；已经生成的文件不会自动搬过去。`, dir, source: outputSource() };
      }
      throw new Error('action 只能是 get / pick / set / reset。');
    },
  }));

  ctx.tools.register(defineTool({
    name: 'paper_prepare',
    description: '解析论文 PDF：抽取文本块与版式、定位图区与图形摘要并裁图、重建表格网格，生成 content.json/source.json。这是翻译流程第一步，只需跑一次；重复调用会复用已有工作目录继续。mode="summary" 时只抽取+裁图、不生成待译项（用户只要总结不要翻译时用这个）。缺依赖时会停下并给出清单——此时先告知用户再决定是否安装，不要自行 pip install。',
    parameters: {
      pdf: { ...str, required: true, description: '本地论文 PDF 的绝对路径' },
      workdir: { ...str, description: '工作目录绝对路径；省略则用 build/papers/<文件名>' },
      mode: { ...str, description: 'translate（默认，全文翻译）或 summary（只总结：不建译文骨架、不产生待译项）' },
    },
    output: asText({ work: str, reused: bool }),
    async execute(args, exec) {
      const pdf = path.resolve(String(args.pdf ?? '').trim());
      if (!pdf) throw new Error('pdf 不能为空。');
      if (!fs.existsSync(pdf)) throw new Error(`找不到 PDF：${pdf}`);
      if (!/\.pdf$/i.test(pdf)) throw new Error(`不是 PDF 文件：${pdf}`);
      const mode = String(args.mode ?? 'translate').trim() === 'summary' ? 'summary' : 'translate';
      const work = args.workdir ? path.resolve(args.workdir) : workDirFor(pdf);
      const reused = fs.existsSync(path.join(work, 'content.json'));
      const argv = ['prepare', pdf, work];
      if (mode === 'summary') argv.push('--mode', 'summary');
      const r = await runPy('run.py', argv, { timeoutMs: 300000, signal: exec?.signal });
      if (r.code === 2) throw fail('缺少 Python 依赖，已停在准备阶段。把下面清单告知用户并等确认，不要自行安装。', r);
      if (r.code !== 0) throw fail(`准备失败（退出码 ${r.code}）。`, r);
      writeState({ lastWork: work, lastPdf: pdf, mode });
      const log = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      return {
        text: [
          mode === 'summary' ? '准备完成（仅总结模式：没有生成待译项）' : (reused ? '复用已有工作目录（续译，不会覆盖已填译文）' : '准备完成'),
          '',
          log,
          '',
          mode === 'summary'
            ? '下一步：paper_summary_brief 取素材包（含图注候选）→ 写五节 + 图解读 → paper_summary_write → paper_summary_doc。不要调用 paper_next/paper_fill。'
            : '下一步：paper_terms 看术语口径 → paper_next 取第一批原文。',
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

  // 和 paper_audit 的分工：audit 逐条对原文查「译得对不对」，这里查「前后译得一样不一样」。
  // 两者都不改文件，可以在 finish 之前任意跑。
  ctx.tools.register(defineTool({
    name: 'paper_consistency',
    description: '术语一致性检查（只读，不改文件）：同一术语在不同批次、不同篇里是否用了两种写法。三类提示——①覆盖率偏低：原文出现过该术语，但多数块没用约定译名（疑似另立写法）；②篇内写法不一致：缩写与中文混用（如 uORF / 上游开放阅读框）；③跨篇不一致：合并成册前需要统一。与 paper_finish 的自检互补：自检看单篇有没有用错，这里看全篇/多篇用齐没、统一没。建议 paper_finish 之前跑一次；多篇一起装订时把所有工作目录一起传。只是提示，不判失败（strict=true 才用退出码表达有提示）。',
    parameters: {
      workdirs: { type: 'array', items: { type: 'string' }, description: '一个或多个工作目录；省略用上一次的目录。多篇一起检查时全部传进来' },
      json: { ...bool, description: '输出 JSON（给程序消费），默认给人看的文字表' },
      strict: { ...bool, description: '有提示即返回失败（退出码 1），默认 false 只提示' },
    },
    output: asText({ works: int, notes: int }),
    async execute(args, exec) {
      let dirs = Array.isArray(args.workdirs) ? args.workdirs.map((d) => path.resolve(String(d))) : [];
      if (!dirs.length) dirs = [resolveWork('')];
      // consistency.py 读不到 content.json 时会静默按空文本处理（扫了但什么都没比），
      // 这里先挡一道，免得「扫描 1 篇、零提示」被误当成本篇没问题。
      const empty = dirs.filter((d) => !fs.existsSync(path.join(d, 'content.json')));
      if (empty.length === dirs.length) {
        throw new Error(`这些目录里没有 content.json，还没开始翻译：${empty.join('、')}。先 paper_prepare 并译完再检查。`);
      }
      if (empty.length) {
        throw new Error(`这些目录里没有 content.json，从检查列表里去掉再跑：${empty.join('、')}。`);
      }
      const argv = ['consistency', ...dirs];
      if (args.json) argv.push('--json');
      if (args.strict) argv.push('--strict');
      const r = await runPy('run.py', argv, { timeoutMs: 120000, signal: exec?.signal });
      const body = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      // 退出码 2 是用法错误（没给路径等）；1 在 strict 下是「有提示」的正常结论，
      // 但非 strict 时冒出来只可能是脚本崩了，得按失败报，别把 traceback 当检查结果。
      if (r.code === 2) throw fail('一致性检查没能执行。', r);
      if (r.code !== 0 && !args.strict) throw fail('一致性检查失败。', r);
      // 提示条数：文字表按「两个空格 + 减号」数，JSON 按三个数组的长度数。
      // 混用会让 json 输出也被判成 0 条（JSON 里没有那种行），尾巴就假报「没有不一致」。
      let notes = (body.match(/^\s{2}- /gm) || []).length;
      if (args.json) {
        try {
          const o = JSON.parse(body);
          notes = ['coverage', 'within', 'across']
            .reduce((n, k) => n + (Array.isArray(o?.[k]) ? o[k].length : 0), 0);
        } catch {
          notes = 0;
        }
      }
      const tail = args.json
        ? ''
        : notes
          ? '—— 这些都是提示，先看「覆盖率偏低」那几条：往往是某几批自己另立了译名。'
          : '—— 没有发现不一致，可以继续 paper_finish。';
      return { text: [body, '', tail].join('\n').trim(), works: dirs.length, notes };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'paper_finish',
    description: '排版生成中文 DOCX 并自检（未译项、图号对应、数字一致性、图表引用、公式误判、图片与表格数量）。退出码含义：通过 / 有疑点 / 还没译完。默认同时导出 PDF（需 LibreOffice 或 Word，耗时 20–40 秒），pdf=false 只出 DOCX。',
    parameters: {
      workdir: { ...str, description: '工作目录；省略用上一次的目录' },
      out: { ...str, description: '输出 docx 绝对路径；省略则落到设置里的输出目录，文件名 <篇名>译文.docx' },
      pdf: { ...bool, description: '同时导出 PDF，默认 true（需 LibreOffice 或 Word）；pdf=false 只出 DOCX' },
    },
    output: asText({ work: str, docx: str, pdf: str }),
    async execute(args, exec) {
      const work = resolveWork(args.workdir);
      contentOf(work);
      const docx = args.out ? path.resolve(args.out) : path.join(outputDir(), `${outStem(work)}译文.docx`);
      const r = await runPy('run.py', ['finish', work, docx], { timeoutMs: 300000, signal: exec?.signal });
      const body = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      if (r.code === 2) {
        return { text: `还没译完，已停下排版（不会输出半成品）。先 paper_status 看剩余，再 paper_next 继续。\n\n${body}`, work, docx, pdf: '' };
      }
      if (r.code !== 0 && r.code !== 1) throw fail(`排版失败（退出码 ${r.code}）。`, r);

      let tail = '';
      let pdfPath = '';
      const wantPdf = args.pdf !== false;   // 默认导出 PDF，只有显式 pdf=false 才跳过
      if (wantPdf) {
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

  ctx.tools.register(defineTool({
    name: 'paper_journal',
    description: '查看这篇论文的过程记录：哪一步出过什么问题、问题定位到哪一条、每个阶段花了多久。多 agent 互相校对时用它对齐口径 —— 同一处问题在日志里有稳定 code + 定位，不必靠复述对账。action=summary 给体检表（耗时 + 问题 + 失败阶段），issues 只列问题（带节点与出现次数，○待处理 ✓已修复 —已豁免），timing 给各阶段耗时占比，decisions 回答「为什么选这张图/这些句子/这些术语」，tail 看最近 30 条原始事件，runs 看每次工具调用的墙钟耗时（带参数和归属，早期没有这两个字段的记录会明确标成「未知」而不是「空」），e2e 给端到端耗时（本地脚本 + 模型翻译 + 模型总结三者相加，模型时间由 next→fill、brief→check 的时间戳差分重建）。timing 默认是累计口径（同一阶段跑两次会相加），要跟上次比必须加 last=true 只取最后一次运行。',
    parameters: {
      workdir: { ...str, description: '工作目录；省略用上一次的目录' },
      action: { ...str, description: 'summary（默认）/ issues / timing / decisions / tail / runs / e2e' },
      last: { ...bool, description: 'timing / issues 用：只算最后一次运行（以 prepare 起点切分），默认累计全部历史；issues 会额外标出「本次新增 / 历史遗留 / 本次已修」' },
    },
    output: asText({ work: str }),
    async execute(args, exec) {
      const work = resolveWork(args.workdir);
      const act = String(args.action ?? 'summary').trim().toLowerCase();
      const flag = ['summary', 'issues', 'timing', 'decisions', 'tail', 'runs', 'e2e'].includes(act) ? act : 'summary';
      const argv = ['journal', work, `--${flag}`];
      if (args.last) argv.push('--last');
      const r = await runPy('run.py', argv, { timeoutMs: 30000, signal: exec?.signal });
      const body = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      if (r.code !== 0) throw fail('读取过程记录失败。', r);
      return { text: body, work };
    },
  }));

  // ---- 文献总结：默认不做，只有用户提到「总结/概括」才触发 ----
  // 用户只要总结、没提翻译时：把 pdf 直接传进来，一步完成「仅总结准备 + 素材包」，
  // 不生成待译项，也就不可能滑进全文翻译。
  ctx.tools.register(defineTool({
    name: 'paper_summary_brief',
    description: '【只总结不翻译的入口】用户要求「总结/概括/归纳/提炼/讲了什么/主要结论/summary」而没提翻译时用它：传 pdf 会在一步内完成轻量准备（只抽取+裁图，不生成待译项）并切出素材包。素材包 ≈1.3 万字符：摘要 + 引言/讨论/结果要点 + 方法小节 + 术语口径 + 图表候选（图注按子图保留、正文引用次数、表格网格）。据此写五节中文总结（目的/方法/难点与解法/创新/局限，难点一节必须写出作者如何解决）+ 1–3 张图的解读，不读全文、不产生译文、不做全文翻译。返回里自带输出格式与硬规则。默认不做总结，没有总结类关键词就跳过。',
    parameters: {
      workdir: { ...str, description: '工作目录；省略用上一次的目录（与 pdf 同时给时以 workdir 为准）' },
      pdf: { ...str, description: '论文 PDF 绝对路径。用户只想要总结时直接传它，省掉单独的 paper_prepare' },
      intent: { ...str, description: '用户这次的原话。传入后若未出现总结类关键词，直接跳过、不生成素材包' },
      budget: { ...int, description: '素材包字符预算，默认 12000' },
      max_figs: { ...int, description: '总结文档最多放几张图/表，默认 3；0 = 纯文字（更快更省）' },
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
      const figs = Math.min(Math.max(Number(args.max_figs ?? 3) || 0, 0), 6);
      let work = '';
      let prep = '';
      const pdf = String(args.pdf ?? '').trim();
      if (pdf) {
        const abs = path.resolve(pdf);
        if (!fs.existsSync(abs)) throw new Error(`找不到 PDF：${abs}`);
        work = args.workdir ? path.resolve(args.workdir) : workDirFor(abs);
        const argv = ['prepare', abs, work, '--mode', 'summary'];
        if (!figs) argv.push('--no-crop');
        const p = await runPy('run.py', argv, { timeoutMs: 300000, signal: exec?.signal });
        if (p.code === 2) throw fail('缺少 Python 依赖，已停在准备阶段。把下面清单告知用户并等确认，不要自行安装。', p);
        if (p.code !== 0) throw fail(`准备失败（退出码 ${p.code}）。`, p);
        writeState({ lastWork: work, lastPdf: abs, mode: 'summary' });
        prep = stripCli(`${p.stdout || ''}${p.stderr || ''}`);
      } else {
        work = resolveWork(args.workdir);
      }
      const n = Math.min(Math.max(Number(args.budget ?? 12000) || 12000, 3000), 20000);
      const r = await runPy('run.py', ['summary', work, '--budget', String(n), '--figs', String(figs)], { timeoutMs: 120000, signal: exec?.signal });
      const body = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      if (r.code !== 0) throw fail('素材包生成失败。', r);
      return {
        text: [
          ...(prep ? ['', prep, ''] : []),
          body,
          '',
          '—— 写完后把 JSON 交给 paper_summary_write（workdir 同上，summary 传 JSON 文本）。',
          figs
            ? `图表：从 [FIG n]/[TAB n] 候选里选 1–${figs} 张承载核心主张的，每张写 why + read + anchor（anchor 必须逐字来自该图图注）。`
              + ' 「其他」里只给了图号、引用次数与图注开头；若其中的图看着比候选更核心，'
              + '先用 paper_grep 传 fig=<图号> 取全文图注再写，不必重切素材包。'
            : '本次 max_figs=0，figures 传空数组即可。',
          '多篇一起总结时：每篇各跑一次 brief + write，最后 paper_summary_doc 传所有 workdir。',
        ].join('\n'),
        work, hit: true,
      };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'paper_grep',
    description: '按关键词回原文检索句子。素材包是按启发式规则挑的，必然有遗漏（实测只覆盖正文的 8%–15%），写五节时若某一节找不到依据（尤其「研究难点与解决方案」缺作者的对策、「局限」缺作者自述、某个结论缺原句），先用它回原文捞，不要把整篇原文塞进上下文，也不要凭印象写。返回的句子与素材包同一套清洗口径，可直接当 evidence 用。传 fig（图号）时改为展开该图/表的完整图注 + 正文引用句——候选池外的图也能取，看到「其他」里某张图值得选就来取。',
    parameters: {
      workdir: { ...str, description: '工作目录；省略用上一次的目录' },
      keyword: { ...str, description: '关键词或正则，如 challenge|difficult|limitation、we developed、cannot predict' },
      fig: { ...str, description: '图号（如 7）。给了它就展开这张图的完整图注，忽略 keyword' },
      limit: { ...int, description: '最多返回几句，默认 20，上限 200' },
      section: { ...str, description: '限定节：results / discussion / intro / methods / abstract，逗号分隔；省略搜全文' },
    },
    output: asText({ work: str, hit: bool }),
    async execute(args, exec) {
      const work = resolveWork(args.workdir);
      const kw = String(args.keyword ?? '').trim();
      const fig = String(args.fig ?? '').trim();
      if (!kw && !fig) {
        throw new Error('keyword 或 fig 至少给一个：检索句子用 keyword（如 "limitation|challenge"），展开某张图的图注用 fig（图号）。');
      }
      const n = Math.min(Math.max(Number(args.limit ?? 20) || 20, 1), 200);
      const argv = fig ? [work, '--fig', fig] : [work, '--grep', kw, '--limit', String(n)];
      const sec = String(args.section ?? '').trim();
      if (sec && !fig) argv.push('--sec', sec);
      const r = await runPy('summarize.py', argv, { timeoutMs: 90000, signal: exec?.signal });
      const body = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      if (r.code === 2) throw fail('检索失败。', r);
      return {
        text: [
          body,
          '',
          fig
            ? '—— 这是该图/表的完整图注。anchor 必须从这里逐字复制（图注原文），不要用正文的句子。'
            : '—— 这些句子可以直接当 evidence（须逐字复制、不得改写）；但别把它们整段搬进总结，五节字数口径不变。',
        ].join('\n'),
        work,
        hit: r.code === 0,
      };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'paper_summary_write',
    description: '提交文献总结：解析 JSON、写入 <work>/summary.json，并当场做机械校验——五节（齐全与字数、难点一节是否写出解法、证据逐字命中原文、数字来自原文、中文纯度、术语是否只写了英文、英文标题可溯源、节间重复）+ 图表（图号是否真实存在、解读字数、anchor 是否逐字来自该图图注、子图字母、是否描述了图注没写的视觉细节）。校验不通过会指出哪一节/哪张图、哪条不达标，改完再提交一次即可。',
    parameters: {
      workdir: { ...str, description: '工作目录；省略用上一次的目录' },
      summary: { ...str, description: '五节总结 + figures 的 JSON 文本（可带 ```json 代码块）' },
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
    description: '把已通过校验的 summary.json 排成【总结】DOCX：五节 + 关键图表（原图/表截图 + 中文解读 + 英文原图注供核对，图注未裁出时自动降级为纯文字）。单篇=一篇；多篇=先目录页（序号 + 中英文标题，带生成日期）再每篇一章，篇间自动分页。可选导出 PDF。文件名自动取「译文名/中文标题 [总结]」，也可用 out 指定。',
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
      // 没指定 out 时由 Python 按输出目录自动命名（单篇用中文标题、多篇用「文献总结_N篇」）。
      const r = await runPy('run.py', argv, {
        timeoutMs: 420000, signal: exec?.signal, env: { PAPER_TRANSLATE_OUT: outputDir() },
      });
      const body = stripCli(`${r.stdout || ''}${r.stderr || ''}`);
      if (r.code === 2) return { text: `总结还没通过必检项，已停下排版。\n\n${body}`, docx: '', pdf: '' };
      if (r.code !== 0) throw fail('总结排版失败。', r);
      const m = /交付:\s*(.+?)（\d+\s*篇）/.exec(body);
      const docx = m ? m[1].trim() : (args.out ? path.resolve(args.out) : '');
      return { text: body, docx, pdf: docx ? docx.replace(/\.docx$/i, '.pdf') : '' };
    },
  }));
}
