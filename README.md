# TaleTone · 声伴

**让故事，用熟悉的声音讲。**

TaleTone 把「本地文件试听」和「本机 AI 制作」分开：在浏览器里给同一份画面切换声音，需要生成配音时再运行自己的电脑。仓库不含演示人物素材、用户视频、参考录音、已生成音频、私人项目或模型权重。

- 在线播放器：[https://papaw.cc](https://papaw.cc)
- 源码：[blockchainshadow/taletone](https://github.com/blockchainshadow/taletone)

## 两种使用方式

| | 公开浏览器播放器 `web/` | 本机编辑器与可选 AI `local/` |
|---|---|---|
| 视频和配音 | 你主动选择的本地文件 | 启动时明确指定视频；页面添加已有音频 |
| 播放 | 原声与外部配音切换、时间偏移 | 原声、已有配音、已生成的声音；本机 FFmpeg 准备 HLS |
| 文件去向 | 浏览器读取，不上传到站点 | 原视频按路径引用；添加的音频复制到自己的项目目录 |
| 文字与项目 | 保存设置/元数据；重开后重新选择匹配媒体 | 保存/导入/导出项目 JSON，编辑已有文字片段 |
| AI | **没有**云端生成、模型或自动连接本机服务 | 可选 Apple Silicon MLX，用户明确点生成才执行 |

公开站点不是 AI 服务，也不是整个本机项目的云端备份。JSON 不包含媒体；两种播放器的项目文件不能视为可互换的模型制作包。目前没有自动转写、自动翻译或一键克隆真人声音。内置生成配置使用模型预设声音与风格指令，不保证方言、情绪、发音或文本还原准确，更不代表任何真人授权或效果承诺。

## 直接用在线播放器

1. 打开 [papaw.cc](https://papaw.cc)，选择自己的视频。
2. 选择已有配音音频，在原声和音轨之间切换；需要时调整时间偏移。
3. 文件只在当前浏览器中读取；刷新/重开页面后按提示重新选择媒体。

推荐 H.264/AAC 的 MP4 视频及浏览器支持的音频。文件扩展名不是解码能力保证；不支持的编码需要你在本机转换。长文件、蓝牙设备、不同采样时钟或浏览器后台节流可能影响外部音轨同步；这不是专业成片验收工具。

自行预览或部署公开版（不需要 Node、构建或 Python AI 依赖）：

```sh
git clone https://github.com/blockchainshadow/taletone.git
cd taletone
python3 -m http.server 8080 --bind 127.0.0.1 --directory web
```

访问 `http://127.0.0.1:8080/`。部署时只发布 **`web/`** 的内容到静态 HTTPS 托管；不要把 `local/`、私人项目目录或运行中的 Python 服务放到公网。

## 本机编辑器：安装与首次启动

需要 Python **3.11+**，以及在 `PATH` 上可调用的 `ffmpeg` 和 `ffprobe`。基础播放器只用 Python 标准库，不需要 `pip install`。macOS 可用 Homebrew 安装：

```sh
brew install python ffmpeg
python3 local/server.py --source "$HOME/Movies/source.mp4" \
  --data-dir "$HOME/.local/share/taletone/my-project" --port 8877
```

把 `source.mp4` 换成你自己有权使用的视频。视频必须包含有限时长的视频流和原声音频流，推荐 H.264/AAC MP4。服务**只绑定 `127.0.0.1`**，打开 `http://127.0.0.1:8877/`。没有 `--host`/公网模式。

全新目录必须显式传 `--source`，初始项目只有一条原声音轨和空的文字片段，不读取任何默认示例或私人路径。原文件不被改写、不复制到仓库。之后可不传 `--source`，从同一目录重新打开：

```sh
python3 local/server.py --data-dir "$HOME/.local/share/taletone/my-project" --port 8877
```

一个目录对应一个源视频；换视频请用新的 `--data-dir`。已有目录遇到不同的显式源路径会拒绝启动，避免悄悄沿用错误项目。不要在登记后移动或替换源文件；备份时需要同时保存原视频、项目目录和自己添加的媒体，JSON 不是媒体备份。

### 已有音频、文字与保存

- 「添加已有配音」把选择的音频上传到 **本机** loopback 服务，最大 512 MiB。探测成功后才能加入项目；不会发送到 papaw.cc。添加后请保存项目。
- 切换声音、修改名称/时间偏移、保存及重新打开保持现有流程。
- 上传音频不会自动产生文字片段。导入自己的项目 JSON 可附带 `segments`；只编辑 `draft_text` 不会改变已有音频，也不会偷偷合成。
- JSON 中的素材 ID 只对当前本机项目目录有效。API 不接受任意文件路径作为项目素材。导入别的目录的 JSON 不会自动找回媒体。

## 可选：Apple Silicon 本机 AI

仅在 **Apple Silicon macOS** 上安装；Intel Mac、Windows、Linux 没有本仓库提供的 MLX 推理后端。公开网页不调用这些依赖。不提供云端、静音或系统语音的假替代；缺少依赖、模型或合成失败时直接显示错误。

建议单独建 Python 3.11/3.12 虚拟环境，并预留数 GB 模型磁盘空间及充足统一内存。以下固定的是现有 worker 的运行时合同，不是跨设备效果/速度保证：

```sh
python3 -m venv .venv-mlx
.venv-mlx/bin/python -m pip install -r local/requirements-mlx.txt
TALETONE_MLX_PYTHON="$PWD/.venv-mlx/bin/python" \
  python3 local/server.py --source "$HOME/Movies/source.mp4" \
  --data-dir "$HOME/.local/share/taletone/my-project" --port 8877
```

默认使用 `mlx-audio==0.5.7`、`mlx==0.32.3` 和 [Qwen3-TTS CustomVoice 1.7B 6-bit](https://huggingface.co/mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit)，固定模型 revision `1c6c0ff58c43afa8df571facde2efa077efd85e2`。模型不随仓库或 pip 安装下载；首次真正合成时由 Hugging Face Hub 获取。请先阅读模型及依赖的许可证和使用条件。下载会访问外部服务器并暴露正常网络元数据，但本实现不把视频、译文或录音发送给推理服务。

配置在**启动本机服务前**设置，不能由导入的项目 JSON 指定解释器或执行代码：

| 环境变量 | 作用 |
|---|---|
| `TALETONE_MLX_PYTHON` | 安装了 MLX 依赖的 Python 可执行文件；默认是启动服务的解释器 |
| `TALETONE_TTS_MODEL` / `TALETONE_TTS_MODEL_REVISION` | 兼容 Qwen3-TTS CustomVoice API/预设声音/24 kHz 输出的模型仓库及固定 revision；更换时必须配套指定并自行验收 |
| `TALETONE_MLX_AUDIO_VERSION` / `TALETONE_MLX_VERSION` | 预期运行时版本；默认见上文。worker 检查实际版本，不会静默忽略差异 |
| `HF_HOME` / `HF_HUB_CACHE` | Hugging Face 模型缓存位置，遵循上游库配置 |
| `HF_HUB_OFFLINE=1` | 禁止 Hub 联网，必须已完整缓存指定 revision；缺失时失败 |

只有 `TALETONE_MLX_PYTHON` 指向另一环境不会替你安装包；修改预期版本也不等于确认新版本兼容。模型/运行时/风格/发布文本参与缓存键，变更后不会错误复用旧声音。

### 从自己的空项目准备生成轨

没有预置译稿。可在项目菜单导出本机项目 JSON，保留原有 `source_sha256`、素材 ID 和原声音轨，在 `tracks` 中追加以下对象，再导入并保存（示例要求源视频至少 4 秒；时间与文字应换成自己的内容）：

```json
{
  "id": "my-mandarin",
  "label": "普通话 · 我的译文",
  "language": "zh-CN",
  "kind": "generated",
  "revision": 1,
  "profile_id": "zh-beijing-v1",
  "timeline_offset_ms": 0,
  "segments": [{
    "id": "1",
    "source_start_ms": 0,
    "source_end_ms": 4000,
    "published_text": "欢迎来到我的故事。",
    "draft_text": "欢迎来到我的故事。"
  }]
}
```

将顶层 `selected_track_id` 改成 `my-mandarin`，或导入后在页面选择它，明确点击「生成并试听」。其他可用配置是 `zh-chengdu-v1`（`zh-CN`）和 `ja-playful-v1`（`ja-JP`）。片段必须按时间排序、互不重叠且位于源视频内。

生成是完整的本机模型推理：24 kHz 单声道，按源视频时间轴排列，片段间静音，不混入原背景音乐。只允许最多 1.2 倍保调加速；文字太长、输出截断、模型缺失或异常不会发布半成品，应缩短文字或扩大时间槽。worker 在内存中分配完整源时长的音频数组，长视频会显著增加内存、时间与临时磁盘需求；先用数秒片段验收，不要假设实时生成。

生成音频、自然语音 take 和 HLS 都是临时缓存，默认约 24 小时 TTL、256 MiB 配额，按访问触发 TTL/LRU 清理，可在页面手动清空。正在读写的条目受保护，空间不足可明确失败。缓存不是永久配音资产；清理后再次试听可能需要重新推理。本项目没有承诺一键导出完整配音成片。

## 隐私与安全边界

- 静态站点无媒体上传、AI API 或分析上报代码；网站托管商仍能看到页面访问的 IP/时间等正常请求信息。浏览器扩展、系统备份与第三方浏览器本身不在此保证内。
- 本机服务是单用户开发型 HTTP 服务：Host 白名单、跨站写入限制和静态路径白名单不是账户隔离或沙箱。不要反向代理、端口转发或绑定公网，不要在不可信多人机器上运行。
- 源路径、媒体名称、SHA-256、译文以及上传副本会写入私有 `--data-dir`；模型另存于 Hub 缓存。`gitignore` 只防误提交，**不是加密、访问控制或擦除承诺**。也不要提交导出的 JSON、日志、截图、个人路径或素材。
- FFmpeg/模型会在本机解析输入；仅处理可信且有权使用的媒体与依赖。声音模仿/克隆需要当事人授权，禁止冒充、欺骗或侵犯版权。

## 开发与验收命令

本机代码及测试在 `local/`；hls.js 1.7.3 随附于 `local/vendor/`，浏览器播放不依赖运行时 CDN。许可证、完整 Apache 2.0 条款和来源记录见 `local/vendor/PROVENANCE.txt`。

下面是一套不接触私人媒体、也不触发 AI 的**全新源视频 smoke 命令**（在仓库根目录执行；它会启动前台服务，结束用 Ctrl-C）：

```sh
SMOKE_DIR="$(mktemp -d)"
ffmpeg -v error -nostdin -f lavfi -i color=c=blue:s=320x180:r=24:d=6 \
  -f lavfi -i sine=frequency=440:sample_rate=48000:duration=6 \
  -map 0:v -map 1:a -c:v libx264 -pix_fmt yuv420p -c:a aac -t 6 \
  "$SMOKE_DIR/source.mp4"
python3 local/server.py --source "$SMOKE_DIR/source.mp4" \
  --data-dir "$SMOKE_DIR/project" --port 8877
```

另一终端运行 `curl --fail http://127.0.0.1:8877/api/project`：应只有 `original` 音轨、`segments: []`。浏览器打开本机页面试听原声，再添加自己准备的音频、切换并保存/重开；不要用公开网站地址访问本机 API。

维护者可在集成后运行行为测试；测试使用临时合成素材，不读私人媒体，默认不下载/推理神经模型：

```sh
python3 -m unittest discover -s local -p 'test_*.py' -v
# 可选：已安装 MLX/numpy 环境时，启用真实 worker 的预缓存 PCM 复用测试。
TALETONE_TEST_MLX=1 TALETONE_MLX_PYTHON="$PWD/.venv-mlx/bin/python" \
  python3 -m unittest discover -s local -p 'test_demand_cache.py' -v
```

PCM 复用测试不证明模型冷启动推理成功。真实神经生成验收需按上面的自有译稿流程执行一次冷生成，查看实际任务状态与试听结果；性能、口音与内容准确度需人工检查。
