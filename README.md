# Yaya Voice — 双端语音链路（一台收声出声，一台出脑子）

手机只负责采集与播放，中间这台「语音机」负责 **VAD / ASR / TTS / 唤醒 / 播放闸门**，
另一台「大脑机」负责 **人格、记忆、历史与生成**。

```
┌──────────┐   PCM16LE 16k, 0.5s/包    ┌─────────────────────────┐   SSE 文本     ┌──────────────┐
│  手机 App │ ─────────────────────────▶ │  语音机 :8780 /voice_edge │ ─────────────▶ │ 大脑机 :8777  │
│ (录音/播放)│ ◀───────────────────────── │  9884 VAD · 9883 ASR     │ ◀───────────── │ /api/voice/… │
└──────────┘   事件 + wav/base64 段      │  9880 TTS · KWS 唤醒      │   逐句文本     └──────────────┘
                                        └─────────────────────────┘
```

## 这套东西解决了什么

1. **听/想循环**——用服务端状态机（idle → wake → recording → thinking → playing）替代客户端按音量猜。
2. **回话慢**——垫音（filler）先出声，正文流式分段推；端到端首段音频 0.7~1.7s。
3. **播放声被当成输入**——播放期由客户端真闭麦 + 服务端 `playing` 标志双保险；VAD 另有
   「短段当噪声」「音乐段不收尾」「静音强制收尾」三道闸。
4. **误唤醒**——KWS 分数只是「像不像」，命中后用 ASR 复核 2.5s 缓冲文本，20ms 判真假。
5. **跨机地址漂移**——大脑在 ICS/热点后面，IP 每次开机可能变；语音机自动扫段发现，
   relay 同样自适应，不需要改配置。

## 目录

| 目录 | 内容 |
|---|---|
| `edge/` | `voice_edge.py`（8780 主入口：唤醒、VAD 转发、垫音、TTS 流式、`/tts` 供第三方）<br>`relay.py`（8778 裸 TCP 中转，自适应目标） |
| `hub/` | `hub.py`（看门狗：开机自启 + 崩溃拉起 + 探活分层）<br>`mailbox.py`（8790 离线信件 + 文件互传） |
| `vad/` | `vad_server.py`（9884 fsmn-vad 流式端点 `/vad_stream`） |
| `docs/` | `protocol.md`（线协议，**改字段前必须双方确认**）<br>`PITFALLS.md`（踩过的坑，比代码值钱）<br>`DEPLOY.md`（部署与自启） |
| `config/` | `*.example.json`（不含任何真实地址/口令） |

## 快速开始（语音机）

```bash
# 1. 准备 Python 3.12 环境
python -m venv venv && venv/Scripts/pip install -r requirements.txt

# 2. 三个模型服务
#    - GPT-SoVITS api_v2.py  (9880, TTS)
#    - FunASR paraformer      (9883, ASR)
#    - FunASR fsmn-vad        (9884, 本仓 vad/vad_server.py)
python vad/vad_server.py                     # 9884

# 3. 大脑地址（二选一：写死或让它自己扫）
export VOICE_BRAIN=http://192.168.137.10:8777
python edge/voice_edge.py                    # 8780

# 4. 看门狗（可选，Windows 计划任务用）
python hub/hub.py --once                     # 单次巡检
```

## 环境变量（全部可覆盖，代码里没有必须改的常量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `VOICE_BRAIN` | 空 | 大脑基址，如 `http://192.168.137.10:8777` |
| `VOICE_ICS_PREFIX` | `192.168.137.` | 大脑地址漂移时扫这个网段 |
| `VOICE_VAD` / `VOICE_ASR` / `VOICE_TTS` | `127.0.0.1:9884/9883/9880` | 三个本地服务 |
| `VOICE_REF` / `VOICE_PROMPT` / `VOICE_PROMPT_LANG` | 空 | TTS 参考音、其文本、语言 |
| `VOICE_KWS_V1` / `VOICE_KWS_HEY` | 空 | openWakeWord 模型路径（留空=关唤醒，只认 `force`） |
| `VOICE_KWS_TH_V1` / `VOICE_KWS_TH_HEY` | `0.93` / `0.99` | 唤醒阈值（**别照抄，按自己的模型标定**） |
| `VOICE_KWS_ASR` | `1` | 命中后是否用 ASR 复核 |
| `VOICE_LP` | `0` | 段内低通截止（Hz），0=关 |
| `VOICE_DUMP_DIR` | 空 | 唤醒留证 wav 落盘目录 |
| `MAIL_KEY` | 空 | 信箱口令（空=拒绝所有写操作） |

## 不包含什么

- **模型权重**（TTS 的 GPT/SoVITS、唤醒 onnx）——体积大且有各自的许可，自行准备。
- **人格与记忆**——在大脑机侧，本仓不涉及。
- **手机 App**——协议见 `docs/protocol.md`，任意能发 PCM 的客户端都可接。

## 许可

MIT，见 `LICENSE`。

## 状态

参考实现，按「能跑、能自愈、坑写清楚」为标准，不追求通用性。
`docs/PITFALLS.md` 里的条目都是真机上踩出来的，读它比读代码省时间。
