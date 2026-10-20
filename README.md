# Interview Helper · 计算机领域面试助手

面向**计算机领域面试与模拟练习**的 Python 桌面工具，覆盖 AI 应用开发、Python、Java、JavaScript、Node.js、全栈开发，以及数据库、网络、操作系统等常见技术面试范围。把一份简历、一份岗位 JD 和项目熟悉度放入对话上下文，让 AI 按真实面试的口语方式回答或提问。

核心功能是**回答者与提问者一键切换**：同一套面试资料，既可以帮助候选人组织答案，也可以让 AI 扮演面试官逐轮追问。

| AI 角色 | 将转写内容视为 | AI 的输出 |
| --- | --- | --- |
| **面试者 / 回答者**（默认） | 面试官的问题 | 直接给出单段口语答案，需要举例时优先使用熟悉且相关的项目 |
| **面试官 / 提问者** | 候选人的发言 | 根据简历、JD 和前文提出一个核心问题或追问，不主动给出参考答案 |

对话区域顶部提供角色切换按钮，例如“面试者 → 切换为面试官”。切换会**暂停监听，并清空当前问答、转写和待处理队列**；已加载的简历、JD、准备状态及项目熟悉度顺序继续保留。点击“开始监听”即可按新角色继续。初始化或生成回复期间暂时不能切换；重启默认回到面试者。

## 功能

- **系统声音实时转写**：从 Windows 的扬声器或耳机输出采集声音，经科大讯飞识别；实时显示文字，在停顿并收到完整结果后自动提交给 AI。
- **深色悬浮窗**：黑底灰白字，支持置顶、不透明度调整、窗口缩放和复制最新回答 / 提问。
- **面试资料直接入上下文**：简历和岗位 JD 分开存为 Markdown，每次请求携带资料、当前角色提示词、项目顺序和问答历史，无需向量知识库或数据库。
- **先确认项目熟悉度**：AI 识别简历项目，用户按“最熟悉 → 最不熟悉”排序并确认，之后才进入准备完成状态。
- **多轮对话与问题排队**：回答期间继续识别后续问题，按顺序处理；失败时支持重试或跳过。
- **口语回答约束**：默认输出单段文字，提示词包含技术边界、事实依据和 few-shot 示例；区分真实经历与“我会如何做”的方案。

模型默认使用 **DeepSeek Flash**（`deepseek-flash`），关闭思考模式。语音识别使用科大讯飞**实时语音转写标准版（RTASR）**。

## 快速开始

桌面语音功能需要 **Windows、Python 3.11 或 3.12**，以及可正常播放声音的输出设备。Python 安装应包含 Tkinter（官方 Windows 安装包通常自带）。

在 PowerShell 中执行：

```powershell
git clone https://github.com/mfn636/interview-helper.git
cd interview-helper
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item context/resume.example.md context/resume.md
Copy-Item context/job_description.example.md context/job_description.md
```

使用 Python 3.12 时，将第一条 Python 命令里的 `-3.11` 改为 `-3.12`。上述复制命令用于首次安装；已有自己的配置和资料时，直接编辑原文件。

随后编辑以下三个文件：

| 文件 | 填写内容 |
| --- | --- |
| `.env` | DeepSeek API Key、讯飞 RTASR 的 APPID 和 APIKey |
| `context/resume.md` | 本次候选人的姓名、简历、项目经历和补充个人背景 |
| `context/job_description.md` | 本次应聘的岗位名称、职责和要求 |

仓库内的两份 `.example.md` **完全虚构，仅供演示**。正式使用前请替换为本次面试的真实资料；三个配置 / 资料文件都使用 UTF-8 编码。

启动悬浮窗：

```powershell
.\.venv\Scripts\python.exe main.py
```

## API 配置

在本地 `.env` 中填写自己的凭据，示例值不可直接使用：

```dotenv
API_KEY=your_deepseek_api_key

XFYUN_APP_ID=your_xfyun_app_id
XFYUN_API_KEY=your_xfyun_rtasr_api_key

SPEECH_SILENCE_MS=1500
SPEECH_RMS_THRESHOLD=180
SPEECH_MAX_SEGMENT_MS=50000
```

讯飞两项凭据必须来自已开通**实时语音转写标准版（RTASR）**的同一个应用，**不需要 APISecret**。语音识别与 DeepSeek 对话分别调用各自的服务，额度和费用以对应平台为准。

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `SPEECH_SILENCE_MS` | `1500` | 连续静音达到多少毫秒后，尝试结束当前语音段 |
| `SPEECH_RMS_THRESHOLD` | `180` | PCM16 音量检测阈值；小声音漏检时调低，背景声误触发时调高 |
| `SPEECH_MAX_SEGMENT_MS` | `50000` | 长音频的内部连接轮转间隔；轮转会拼接文字，不因达到该时长就提前提交问题 |

## 一次面试的使用流程

1. **加载资料**：启动后读取当前角色提示词、简历和 JD，调用 AI 识别简历中的项目。
2. **确认项目顺序**：在排序窗口选择项目，用“上移 / 下移”按熟悉度排列，再点击“确认顺序并准备”。一个项目也需确认；没有识别到项目时，先检查简历，或明确确认本次无项目。
3. **等待准备完成**：AI 回复“我准备好了，面试人：xxx，面试岗位：xxx”后才可开始监听。点击“稍后确认”会暂缓准备，可从主窗口重新打开项目排序。
4. **选择角色与声音来源**：默认面试者，可用对话区顶部按钮切换为面试官。选择需要转写的声音实际播放的输出设备，默认跟随 Windows 默认输出。
5. **开始监听**：点击“开始监听”。临时转写会实时修正，停顿后完整文本进入待处理队列，由 AI 按当前角色回复。

两种角色都采集**所选系统输出的声音**，不会因为切换角色而改用麦克风。面试官模式下，应让候选人的发言通过所选输出设备播放；希望自己直接输入文字时，可使用下方的命令行模式或 Python 客户端。

项目顺序只在本次运行内保存，重启后重新确认。回答需要举例时，优先选熟悉且相关的项目；问题明确点名项目或延续上轮追问时，以问题所指项目为准。排序不会改写简历，也不应把 JD 要求当作已经做过的经历。

准备完成前修改了资料，可点击“重新加载资料”，重新识别并确认项目；普通初始化重试会保留已确认的顺序。准备完成后修改资料，请重启程序。

### 监听与会话控制

- **暂停监听**：停止采集与自动提交，舍弃尚未完成的语音片段，保留已完整转写的待答问题。正在生成的回复可以完成，恢复监听后继续处理队列。
- **重试 / 跳过失败问题**：请求失败后暂停后续自动提交；失败问答不记入历史。暂停监听期间点击重试，会等恢复监听后再发送。
- **新对话**：停止监听，清空问答、转写和队列，保留资料、项目顺序及当前角色。旧请求的迟到结果不会混入新对话。
- **刷新设备**：更新系统输出设备列表；更换输出前先暂停监听。
- **窗口操作**：拖动系统标题栏移动，拖动边缘缩放；支持置顶、65%～100% 不透明度及复制最新回复。关闭窗口会停止采集并退出。

只查看设备列表，不开始录音：

```powershell
.\.venv\Scripts\python.exe main.py --list-audio-devices
```

## 命令行与 Python 调用

命令行模式使用手动文字输入，不启动声音采集，默认使用面试者角色：

```powershell
.\.venv\Scripts\python.exe main.py --cli
```

按提示输入完整项目编号顺序，例如 `2 1`；一个项目输入 `1`，无项目输入 `none`。`/clear` 清空历史并保留资料与项目顺序，`/exit` 退出。CLI 当前没有角色切换命令；交互式切换使用悬浮窗，也可在 Python 中指定角色：

```python
from deepseek_client import DeepSeekClient
from loop import parse_project_order

client = DeepSeekClient(role="interviewer")  # candidate 为回答者，interviewer 为提问者
projects = client.detect_projects()
for number, name in enumerate(projects, 1):
    print(number, name)

order = parse_project_order(input("按熟悉度输入全部编号，无项目输入 none："), projects)
client.set_project_order(order)
print(client.initialize())
print(client.chat("请开始本次模拟面试。"))
```

`DeepSeekClient(resume_path="简历.md", jd_path="岗位.md")` 可分别指定资料路径。直接调用 `client.set_role("candidate")` 或 `client.set_role("interviewer")` 切换时，调用方需清空自行维护的历史；悬浮窗会自动处理。

## 目录与提示词

```text
main.py                            程序入口
floating_window.py                 Tkinter 悬浮窗与角色切换
floating_session.py                对话状态与后台任务
deepseek_client.py                 DeepSeek 请求、项目识别与角色提示词
project_order_dialog.py            项目熟悉度排序窗口
speech_session.py                  语音会话
system_audio.py                    Windows 系统声音采集
xfyun_rtasr.py                     讯飞 RTASR 协议
voice_controller.py                语音文本与 AI 问答队列
loop.py                            命令行多轮对话
prompts/interview_assistant.md      面试者 / 回答者提示词
prompts/interview_interviewer.md    面试官 / 提问者提示词
context/resume.example.md          虚构简历模板
context/job_description.example.md 虚构 JD 模板
.env.example                       不含真实密钥的配置模板
benchmarks/questions.json          20 道计算机面试题与评分要点
```

两份角色提示词可独立修改。回答者提示词要求围绕“结论 → 原理 → 必要边界”自然口述，核对技术版本与适用条件，避免编造个人经历；面试官提示词要求结合岗位与发言逐轮提问、追问。客户端会合并多余换行，供显示、复制和后续对话使用，不硬截断回答。提示词约束不保证技术结论正确。

## 测试与当前限制

当前版本已有 **167 项离线测试通过**，覆盖客户端角色、项目确认、资料加载、语音协议、分段、队列和旧结果隔离。运行测试不调用真实 API：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

系统声音 → RTASR → DeepSeek 的**真实语音全链路尚未稳定验证**。联调出现过握手超时、连接中断和服务端 `10700` 引擎错误；离线测试通过不等于所有设备和网络下都可稳定运行。异常断连或未稳定的文字不会自动提交给 AI。建议先用本机设备和自己的配置验证转写，再开始完整模拟面试。

20 题基准测试先可离线校验题库：

```powershell
.\.venv\Scripts\python.exe benchmark.py --validate
```

实际测评会调用 API，须显式确认项目熟悉度顺序。以下示例仅适用于识别出两个项目、且第二个更熟悉的情况：

```powershell
.\.venv\Scripts\python.exe benchmark.py --project-order "2,1"
```

编号必须覆盖本次识别出的全部项目；一个项目用 `1`，无项目用 `none`。结果写入 `benchmarks/results/`，记录完整响应耗时、回答及 Token 用量；当前接口为非流式，不测首 Token 延迟。答案正确性需按题库评分要点人工审阅，脚本不自动给出正确率或分数。

## 私有资料与数据流

仓库仅提供代码、提示词和虚构示例。真实 `.env`、本次简历和 JD、原始资料与基准测试结果应留在本地；相应默认路径已加入 `.gitignore`。不要把真实密钥写进提示词、截图、Issue 或提交记录；自行新增的私有文件也应加入忽略规则。

开始监听后，所选输出设备上的声音会发送给讯飞识别，包括该设备播放的其他音频。转写文字、简历、JD、项目顺序和问答历史会发送给 DeepSeek。程序不将录音音频写入磁盘；问答历史和项目排序保存在内存中，退出后不保留。基准测试会另外保存回答结果。

资料和历史目前不会自动截断；长对话可能增加延迟、费用或触及模型上下文限制，可使用“新对话”清空历史。涉及个人经历或关键技术结论时，请结合自己的事实与理解核对生成内容。

## 许可证

本项目采用 [MIT License](LICENSE)。
