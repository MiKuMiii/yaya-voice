# Credits 与出处

本仓库自己的代码以 MIT 发布（见 `LICENSE`）。下面三块是**别人的东西**，分三类记：
「运行时依赖」「模型与权重」「思路与文档」。类别不同，义务也不同。

标注 **待核** 的条目，是我这边没拿到可信确认的，合并前请按右列链接自己看一眼。

## 〇、本项目由两台机器共同运行

这不是一个人一台机器写出来的东西：**边缘侧跑在台式主机上（音频：唤醒 / VAD / ASR / TTS / 状态机 /
看门狗），大脑侧跑在笔记本上（人格 / 记忆 / 历史 / 声纹）**，两边同时改、同时验。分工与理由见 README
的「谁在维护这套东西」一节。仓库里的每一份协议文档都是两边对着真机时间戳核过的，单人复现时
可以按 README 的「快速开始」把两侧合并到一台机器上跑，只是会失去这种跨机验证。

---

## 一、运行时依赖（代码）

这些是「装在你机器上、调用它」的库，本仓库不包含它们的源码。分发方式取决于你打不打进发布包。

| 组件 | 用途 | 许可证 | 说明 |
| --- | --- | --- | --- |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | 声纹 / VAD / KWS 推理 | Apache-2.0 | GitHub API `/license` 已确认 |
| [openWakeWord](https://github.com/dscripka/openWakeWord) | 唤醒词模型运行时 | Apache-2.0 | 已确认 |
| [RVC](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) | 音色转换（供你自己训自己的音色） | MIT | 已确认 |
| [3D-Speaker](https://github.com/modelscope/3D-Speaker) | 说话人向量（campplus） | Apache-2.0 | 已确认；**模型权重另算**，见下 |
| `pyaec`（PyPI） | WebRTC AEC 的 Python 绑定 | **待核** | 不在 GitHub 同名仓库，请查 PyPI 页面 |
| `PyAudioWPatch`（PyPI） | WASAPI 环回采集 | **待核** | 同上 |
| [edge-tts](https://github.com/rany2/edge-tts) | 可选 TTS 回退 | **待核**（项目自称 GPL-3.0，API 返回 Other） | 本项目**只作为外部调用**，不分发其代码；如果你要把它打进包里，请自行确认 GPL 的传染范围 |

## 二、模型与权重（不进仓库）

见 `MODELS.md`。一句话：**仓库里不含任何权重文件**，全部由使用者自己按官方渠道下载。
特别地，本项目不附带、也不得附带任何**商业音色**（例如以初音未来为参考训练出来的 RVC / SoVITS 权重）——
音色不是我们的，能跑只是本地自用，公开分发就是另一回事了。

## 三、思路与文档借鉴

想法本身不受版权保护，但别人写下来的文档受。下面这些是**参考了思路**，文字全部重写，没有复制段落：

| 来源 | 借鉴了什么 | 许可证 |
| --- | --- | --- |
| [dsh-orb-cordis](https://github.com/mini-yifan/dsh-orb-cordis)（`docs/07-voice-listen.md` 等） | 「播报期只认口令、其余丢」「退下/插话两类口令」「唤醒回复那句不进对话」这三条听法规则 | MIT |
| [ops-handoff-design](https://github.com/kira905/ops-handoff-design) | 改文件的安全纪律（dry-run 先行、命中数不为 1 就拒、三态不许静默成功）——本项目只借它的原则，未复制正文 | **CC BY-NC-SA 4.0**（仅文档；若要引用其原文段落，必须带该许可且限非商用） |
| 各家 ASR/TTS 的 README 与示例 | 接口形状与参数名 | 各自项目许可 |

## 四、明确不放进仓库的东西

- 任何 `.pth` / `.onnx` / `.ckpt` / `.index` 权重
- 任何真实录音、语音样本、声纹库
- 任何 API key、口令、cookie、内网 IP、局域网设备名
- 抓取来的第三方内容（B 站字幕/弹幕、书源清单）
- 游戏相关的任何东西（本仓库只做语音，不碰游戏）

`sanitize.py` 会对前四类做扫描；最后一类靠自觉。
