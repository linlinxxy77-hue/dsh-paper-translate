# 给 Agent 的安装与使用指引

> 本文件写给 DeepSeek Harness 里的 Agent（大模型）看。当用户询问「如何安装 / 如何使用 / 为什么用不了 dsh-paper-translate」时，请先读本文件，按下面的步骤引导用户，不要凭印象编命令。

## 一、这个插件是什么

`dsh-paper-translate` 是一个 DeepSeek Harness 插件，把英文论文 PDF 翻译成中文 DOCX，并尽量保留原版式（插图、图注、三线表、公式、参考文献原文不译），还可生成文献总结。它封装了 16 个工具，翻译工作流由 Agent 按「取原文 → 回填译文」的固定工序驱动。

## 二、判断插件是否已安装

先确认插件是否可用。如果当前会话里能看到以下工具之一，说明插件已加载：

- `paper_doctor`（体检环境）
- `paper_prepare`（解析 PDF）
- `paper_status`（查看进度）

如果这些工具都不存在，说明插件未安装，进入第三节。

## 三、正确安装插件（按顺序做，做完一步再做下一步）

### 前提

- 用户已安装 DeepSeek Harness（桌面版）。
- 用户已安装 Python 3.10 或更高版本（安装时勾选 "Add Python to PATH"）。

### 步骤 1：安装插件本体

在用户电脑上执行（把命令原样交给用户，让其打开 PowerShell / CMD 后粘贴）：

```bash
dsh plugin --profile desktop add dsh-paper-translate
```

说明：
- 这条命令会自动装 npm 包并写入 `dsh.profile.bundles`，不需要手动改配置文件。
- 如果 `dsh` 命令不在 PATH 里，它在 DSH 安装目录下：`resources/runtime/cli/bin/dsh.cmd`，用完整路径调用即可。
- 备选（手动方式，需多改一步配置）：

```bash
cd %USERPROFILE%\.dsh\profiles\desktop
pnpm add dsh-paper-translate
```

然后在同一目录的 `package.json` 的 `dsh.profile.bundles` 数组末尾加一行 `"dsh-paper-translate"`。

### 步骤 2：重启 Harness

安装后必须**完全退出** DeepSeek Harness（不是刷新窗口、不是重新加载会话），再重新打开。否则新工具不会注册。

### 步骤 3：安装 Python 依赖

插件翻译时依赖三个 Python 包：`pymupdf`、`python-docx`、`pillow`。引导用户在插件目录执行：

```bash
cd %USERPROFILE%\.dsh\profiles\desktop\node_modules\dsh-paper-translate
python python\env_check.py --root . --venv .venv --bootstrap
```

### 步骤 4：验证

让用户重新开一个会话，说「帮我检查论文翻译环境」。此时应能看到 `paper_doctor` 工具并运行它；返回「环境就绪」即为安装成功。

## 四、常见问题（按此排查，不要瞎猜）

| 现象 | 原因与处理 |
|------|-----------|
| 工具列表里没有 paper_* 工具 | 插件未装或未重启；回到第三节步骤 1、2 |
| 报 `python` 不是命令 | 没装 Python 或没加 PATH；先装 Python 3.10+ 并勾选 Add to PATH |
| 报缺 pymupdf / docx / pillow | 依赖没装；回到步骤 3 |
| `dsh plugin add` 很慢或失败 | 国内网络问题；建议用户先配 npm 镜像源（如 npmmirror），再重试 |
| 翻译能跑但导不出 PDF | PDF 导出需要 Word 或 LibreOffice；可先只交付 DOCX，PDF 为可选 |
| 装完还是提示缺依赖 | 插件 .venv 没建成功；让用户删掉 `.venv` 目录后重跑步骤 3 |

## 五、翻译流程（插件已装好后）

1. `paper_prepare` —— 解析 PDF、裁图、建骨架（第一步，只跑一次）。
2. `paper_terms` —— 查看术语表。
3. 循环：`paper_next`（取原文）⇄ `paper_fill`（回填译文），直到 `paper_status` 显示完成。
4. `paper_audit` / `paper_consistency` —— 抽检质量。
5. `paper_finish` —— 排版成 DOCX（可选导出 PDF）。
6. `paper_harvest` —— 沉淀新术语。

用户只想「总结」不翻译时，走 `paper_summary_brief` → `paper_grep` → `paper_summary_write` → `paper_summary_doc` 这条链路。

## 六、给 Agent 的铁律

- **不要自行 pip install / npm install 到系统全局**：装依赖走 `env_check.py --bootstrap`，它会把依赖装进插件的 `.venv`，不污染用户环境。
- **缺依赖时先告知用户**，征得同意再操作，不要静默安装。
- **不要改用户的 DSH profile 配置文件**：对账 bundles 交给 `dsh plugin add` 命令完成。
- 遇到本文件没覆盖的问题，优先查插件自带的 README.md，其次再向用户复述现象求助。
