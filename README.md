# PLAY CLI

**让 Agent 把乐谱变成你能听见的钢琴。**

PLAY CLI 是面向 Agent 的音乐工具：由 Agent 读谱并处理听后反馈，再通过 CLI 验证 MusicXML、生成完整 WAV。用户不需要懂乐理，也不需要手动编辑音符。

> “把这张简谱弹给我听。”
>
> “后面听起来不对，再检查一下。”

它做的是**代奏，不是代唱**。CLI 本身不识别图片，也不会自动编配伴奏。

[快速开始](#快速开始) · [常用命令](#常用命令) · [音色包](#音色包) · [开发](#开发) · [许可证](#许可证)

## 如何工作

```text
简谱 / 五线谱图片或 PDF
          ↓
Agent：读谱、校对
          ↓
可编辑的 MusicXML
          ↓
PLAY CLI + 本地 Sound Pack：验证、渲染
          ↓
完整 WAV + 可追溯的 Result Bundle
          ↑
用户听后反馈 → Agent 重新检查原谱 → 新版本
```

已有 MusicXML？可以直接使用 CLI，跳过图片解释步骤。

## 快速开始

### 1. 安装

需要 **Python 3.11+ 和 macOS**。从源码安装时，将下面路径替换为实际检出目录：

```bash
cd /absolute/path/to/play-cli
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/playcli doctor
```

可执行文件名是 `playcli`，不是 `play`；不会覆盖 SoX 的同名命令。

- **不需要 Docker 或 Colima**。
- FFmpeg / ffprobe 只用于音色包转换和相关开发测试，不是验证、渲染的运行依赖。
- 即时播放是可选能力，macOS 使用 `afplay`。没有播放器也能生成 WAV。
- Linux 兼容实现尚未验证，建议使用 macOS。

### 2. 生成第一个 WAV

以下示例使用仓库内的 CC0 合成测试音色，无需下载生产采样。**它只证明流程可运行，不代表真实钢琴音质**。

```bash
cd /absolute/path/to/play-cli

# 使用新目录，并解析 macOS 临时目录中的路径别名。
demo_root=$(.venv/bin/python -c 'import pathlib,tempfile; print(pathlib.Path(tempfile.mkdtemp(prefix="playcli-demo-")).resolve())')

.venv/bin/playcli sound-pack install tests/fixtures/performance-test-piano-pack \
  --store "$demo_root/packs"

.venv/bin/playcli validate tests/fixtures/supported-monophonic.musicxml

.venv/bin/playcli render tests/fixtures/supported-monophonic.musicxml \
  --sound-pack play-cli-performance-test-piano \
  --sound-pack-version 1.0.0 \
  --sound-pack-store "$demo_root/packs" \
  --output "$demo_root/result"

open "$demo_root/result/performance.wav"
```

也可以在 `render` 命令末尾添加 `--play`，请求生成后立即播放。播放状态与持久化音频结果分开报告。

### 3. 查看结果

```text
result/
├── performance.wav   # 完整演奏
├── voice-*.wav       # 各个原始声部；多声部结果中提供
├── score.musicxml    # 本次采用的乐谱解释
├── validation.json  # 验证结果、默认值与警告
├── schedule.json    # 规范化音符与时间安排
└── result.json      # 产物哈希、版本、来源与修订历史
```

结果目录是只读快照，不会覆盖已有输出。需要修正时，复制 MusicXML 到新的工作文件；不要直接修改历史 Bundle。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `playcli doctor` | 检查运行环境与可选播放能力 |
| `playcli validate <score.musicxml>` | 验证乐谱并返回事件、默认值与警告 |
| `playcli sound-pack install <local-directory> --store <store>` | 从明确的本地来源安装音色包 |
| `playcli sound-pack list --store <store>` | 列出音色包 |
| `playcli sound-pack verify <id> --version <version> --store <store>` | 核验指定音色包的完整性 |
| `playcli render <score.musicxml> --sound-pack <id> --sound-pack-version <version> --sound-pack-store <store> --output <bundle>` | 生成完整 Result Bundle |

当前响应 schema 为 `1.6`，可显式传入 `--schema-version 1.6`；Result Bundle schema 为 `1.2`。Agent 应解析 stdout 中的单个 JSON 对象，检查退出码、`status`、`warnings` 和稳定的 `error.id`。失败返回非零退出码；带非致命警告的完整演奏仍可成功。

精确时间以事件中的 `onset_quarters_exact`、`duration_quarters_exact` 有理数为准。CLI 验证的是明确支持的 MusicXML 子集，不是完整 MusicXML XSD 验证器。

修订演奏时，向完整的 `render` 命令追加 `--previous-result <previous-bundle>` 和 `--revision-reason "<reason>"`，并选择同一父目录下的新输出目录。历史版本不被改写。

## 音色包

真实钢琴演奏需要独立音色包。Standard Piano 基于 **Salamander Grand Piano V3**，不随仓库或 wheel 分发。

请先取得经过审核的本地音色包，再通过 `sound-pack install` 安装。缺少音色包时 CLI 返回明确错误，不会从网络自动补齐。音色包存储路径不能包含已有符号链接。

测试音色不能替代生产音色；使用或分发音色包时需保留其许可、署名和来源记录。

## 开发

```bash
cd /absolute/path/to/play-cli
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m ruff check src tests tools
.venv/bin/python -m mypy --strict src tools
.venv/bin/python -m pytest
```

完整测试还涉及音色包转换环境；相关构建测试使用固定的 FFmpeg / ffprobe 9.0.1。仅运行 CLI 不需要它们。

构建 wheel 和源码分发包：

```bash
cd /absolute/path/to/play-cli
build_root=$(mktemp -d)
.venv/bin/python tools/build_distributions.py \
  --output "$build_root/distributions" --source-date-epoch 1789142400
```

可复现构建要求固定的 `build==1.6.0` 与隔离后端 `setuptools==84.0.0`。

## 许可证

代码与随附文档使用 [Apache-2.0](LICENSE)。音色包及其采样遵循各自许可证，不属于代码许可证的授权范围；Salamander 采用 CC BY 3.0，仓库内测试音色独立采用 CC0-1.0。

加入或分发音频资产前，请阅读 [授权边界](LICENSES.md) 及相应音色包内的许可、署名和来源记录。
