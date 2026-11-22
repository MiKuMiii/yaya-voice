# 模型清单（不随仓库分发）

这个方案要用到几类模型。**仓库里一个都不含**，请按下面渠道自己下，放到你自己定的目录，用配置指过去。

## 唤醒词

- 运行时：[openWakeWord](https://github.com/dscripka/openWakeWord)（Apache-2.0）
- 模型：预训练的几个（如 `hey_jarvis`）可以直接用；想要自定义唤醒词，用它的训练脚本自己训
  - 训练需要正样本 + 负样本 + 背景噪声，几十条到几百条即可
  - 训出来的 `.onnx` 是你自己的产物，怎么放随你
- **不要**把别人训好的、带特定人名/角色的唤醒词模型塞进公开仓库

## 说话人向量（声纹）

- 运行时：[3D-Speaker](https://github.com/modelscope/3D-Speaker)（Apache-2.0）
- 模型：`campplus` 中文通用版，从官方 release 下载（约 27 MB）
- 用你自己的录音建底库；底库文件（`*.json` 里的向量）属于个人生物特征，**不要提交**

## 语音识别（ASR）

三种都能用，按机器选：

- [FunASR](https://github.com/modelscope/FunASR)：`paraformer-large` + `fsmn-vad` + `ct-punc`（中文效果好，需 GPU）
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)：`small` 起（CPU 可跑）
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)：自带多种 ASR 模型，端侧友好

模型文件都很大（几百 MB 起），请自行下载，不要进仓库。

## 语音合成（TTS）

- [GPT-SoVITS](https://github.com/RVC-Project/GPT-SoVITS)：先训「音色」，再用 `ref_audio_path` + `prompt_text` 推理
  - **参考音必须是你能合法使用的声音**；用它人的声音（包括虚拟歌手）做公开分发，风险自负
- edge-tts：微软在线合成，作为回退；注意服务条款
- 任何本地 TTS 都可以，只要最后能吐出 wav（16k 或 32k 单声道 16bit）

## 音色转换（RVC，可选）

- [RVC](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)（MIT）
- 只在你想把 TTS 的输出"变个音色"时用；训练数据同样必须是你能用的声音
- **本项目不附带任何 RVC 权重**

## 一句话原则

> 代码可以开源，模型要自己下，声音得是你的。
