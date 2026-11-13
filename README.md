# dsh-paper-translate

[![npm](https://img.shields.io/npm/v/dsh-paper-translate)](https://www.npmjs.com/package/dsh-paper-translate)

把英文论文 PDF 译成中文 DOCX，**按原版式重排**：插图、图注、三线表、公式、参考文献位置不变，参考文献正文保留英文不译。

这是 paper-translate 技能流水线的 DeepSeek Harness 插件封装 —— 插件是壳，真正的解析、重建、校验逻辑在 `python/` 目录里。

## 它和「让 AI 直接翻译」的区别

| | 直接对话翻译 | 本插件 |
|---|---|---|
| 版式 | 打散成 Markdown | 按 PDF 原始坐标重建，双栏、跨栏、页眉页脚都还原 |
| 图片 | 丢失 | 按图注编号裁切嵌入，图形摘要单独识别 |
| 表格 | 变成文本 | 重建网格，`header`/`rows` 对齐原表 |
| 参考文献 | 一并译掉（往往译错） | 骨架预填英文原文，跳过不译 |
| 术语 | 每段各译各的 | 全文一张术语表，回填时逐条检查 |
| 数字 | 可能改写成中文数字 | 逐项比对，缺一个就点名到段落 |
| 抽检 | 无 | 按风险打分挑段落，原文译文并排复核 |
| 总结 | 每次重读全文，口径不一 | 素材包 + 证据锚点，机械校验后才出稿 |

代价是慢：一篇 13 页的论文约 10 分钟，绝大部分时间花在逐段翻译本身。

## 安装

### 1. 安装插件

```bash
cd ~/.dsh/profiles/desktop          # Windows: %USERPROFILE%\.dsh\profiles\desktop

pnpm add dsh-paper-translate        # 从 npm 安装（推荐）
pnpm add dsh-paper-translate@0.1.0  # 锁定版本
pnpm add github:linlinxxy77-hue/dsh-paper-translate   # 从 GitHub 源码
pnpm add link:<插件目录绝对路径>      # 本地开发
```

然后在同一个 `package.json` 的 `dsh.profile.bundles` 数组末尾加一行：

```json
"dsh-paper-translate"
```

重启 Harness（完全退出再打开，不是刷新窗口）。

> npm 上的版本是预构建产物，安装时不需要编译，也不用点 `allowBuilds` 授权；从 GitHub 源码安装会走 pnpm 的源码拉取，国内网络更慢。

### 2. 安装 Python 依赖

```bash
cd node_modules/dsh-paper-translate   # Windows: node_modules\dsh-paper-translate
python -m venv .venv
.venv/bin/pip install -r python/requirements.txt        # Windows: .venv\Scripts\pip
```

需要 Python 3.10+，三个依赖共约 25 MB：`pymupdf`、`python-docx`、`pillow`。

插件也会自己找项目内的 `.venv`，装在别处的话用 `config.json` 的 `pythonPath` 指过去。

### 3. 验证

对模型说「检查论文翻译环境」，它应该调用 `paper_doctor` 并报告三个依赖是否就绪。

## 使用

说一句「把这篇论文译成中文，保留图表」即可，模型会自己走完：

```
paper_prepare  →  paper_terms  →  paper_next  ─┐
                                      ↑        │
                                  paper_fill  ─┘   （分批循环）
                                       ↓
                              paper_audit  →  paper_finish
```

| 工具 | 作用 |
|---|---|
| `paper_doctor` | 检查 Python、虚拟环境、依赖 |
| `paper_prepare` | 解析 PDF，生成骨架、裁图、表格网格（`mode=summary` 时跳过骨架） |
| `paper_terms` | 列出本文术语的约定译名 |
| `paper_next` | 取下一批原文（默认 20 条） |
| `paper_fill` | 回填译文，当场做数字与术语检查 |
| `paper_status` | 进度与下一批序号 |
| `paper_audit` | 语义抽检，原文译文并排 |
| `paper_finish` | 排版生成 DOCX、自检，可选导出 PDF |
| `paper_harvest` | 抽取新术语候选，用于沉淀词表 |
| `paper_summary_brief` | 只总结不翻译的入口：传 pdf 一步完成轻量准备 + 素材包 |
| `paper_summary_write` | 提交五节总结 + 图表解读，11 项机械校验 |
| `paper_summary_doc` | 生成【总结】DOCX/PDF（含关键图表），多篇可合并 |

工作目录默认落在 `~/paper-translate-work/build/papers/<篇名>/`，同一篇重复准备会复用，不会覆盖已填译文。

### 文献总结（可选，默认不做）

**只有用户说「总结 / 概括 / 归纳 / 提炼 / 讲了什么」才会触发**，普通翻译请求不会顺手多出一步。
反过来，用户只想要总结、没提翻译时，**不生成任何待译项**，自然也就不会跑全文翻译。

| 用户说 | 走什么 | 产出 |
|---|---|---|
| 翻译这篇 | `paper_prepare` → `next/fill` → `finish` | 译文 DOCX/PDF |
| 概括 / 总结 / 这篇说了什么 | `paper_summary_brief`（直接传 pdf）→ `write` → `doc` | 只有【总结】DOCX/PDF |
| 翻译，再总结一下 | 翻译全流程 + 总结 | 两份 |
| 总结，别翻译 | 同第二行 | 只有总结 |

```
paper_summary_brief  →  paper_summary_write  →  paper_summary_doc
  轻量准备 + 素材包         五节+图 + 11 项校验      【总结】DOCX/PDF
```

- 五节：研究目的 / 研究方法 / 研究难点 / 创新点 / 局限，合计 370–710 字
- 每节附一句原文证据，`paper_summary_write` 会要求它**逐字命中原文**——编不出引文就过不了
- 素材里确无依据时写「原文未明确说明」，不猜
- 多篇：把多个 workdir 一起传给 `paper_summary_doc`，生成目录 + 每篇中英文名、五节与图表
- 成本：单篇约 2k token（摘要级）；带 3 张图约 4.5k（约为整篇翻译的 1/10），1–2 分钟

### 关键图表

`paper_summary_brief` 的素材包里带图表候选（默认 3 张，`max_figs=0` 关闭）：

- **候选怎么挑**：方法示意图、正文引用最多、有网格的对比表、末图各保一个，再按分数补位；图形摘要与无主图区不参与
- **喂什么**：图注按子图（panel）保留，加正文里引用该图的句子、表格的表头与行列数——**不喂图片像素**
- **写什么**：每张 `why`（为什么值得看）+ `read`（先答问题、再按子图顺序说看了什么、最后落到支撑哪个结论）+ `anchor`
- **检查**：图号必须真实存在；`anchor` 必须逐字来自该图图注；提到的子图字母图注里要有；数字只能来自全文或该图图注；**描述了图注没写的视觉细节（颜色/曲线/坐标轴）会被点名**
- **排版**：原图截图 + 中文解读 + 灰色英文原图注（供核对）；图注没裁出来时降级为纯文字

这样解读的准确上限由图注决定：Nature/Cell 式图注把每个子图的样本量、统计检验都写了，能讲透；句式图注只够讲整图结论，这时不写子图。

## 配置

复制 `config.example.json` 为 `config.json`（已在 `.gitignore` 中，不会提交）：

| 字段 | 说明 |
|---|---|
| `projectRoot` | 数据根目录，默认 `~/paper-translate-work` |
| `pythonPath` | 指定解释器，默认自动找 `.venv` |
| `skillDir` | 指向外部脚本目录，用于跟技能仓库联动 |
| `dataDir` | 状态文件目录，默认插件内 `data/` |

也可以用环境变量覆盖：`DSH_PAPER_PROJECT`、`DSH_PAPER_PYTHON`、`DSH_PAPER_SKILL`、`DSH_PAPER_DATA`。

## 目录结构

```
src/index.js     工具定义（12 个）
src/runner.js    spawn Python，UTF-8 与超时处理
src/paths.js     路径解析（环境变量 → config → 内置默认）
python/          解析、重建、校验流水线（从 paper-translate 技能同步）
```

`python/` 是技能脚本的副本，行为固定可复现。技能更新后需要手动同步一次。

## 已知限制

- **扫描版 PDF 不支持**：没有文字层就抽不出段落，先做 OCR。
- **图形摘要靠判据识别**：图注文本 + 位置/面积打分，仍可能误判，可用 `figs_override.json` 手工指定裁切框。
- **表格重建会失败**：行列数异常时标 `_manual`，需要手工填 `header`/`rows`，不会瞎猜。
- **PDF 导出依赖外部程序**：优先 Microsoft Word（COM），否则 LibreOffice；两者都没有时只出 DOCX。
- **图表解读不看图**：只读图注与正文引用句，所以图注没写的视觉细节（颜色、曲线走势）一律不写；个别 PDF 的字距伪影（`Researc h`、`( A )`）会原样留在英文图注里，括号已自动修。
- **图注质量决定上限**：一句话图注的论文只能给「这张图证明了什么」，讲不到子图层面。

## License

MIT
