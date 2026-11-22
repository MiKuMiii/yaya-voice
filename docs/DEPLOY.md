# 部署（Windows 实机跑出来的流程）

## 0. 你需要自己准备的

| 组件 | 说明 |
|---|---|
| GPT-SoVITS | TTS，起 `api_v2.py` 在 9880。本仓只调它，不含权重 |
| Paraformer（可选） | ASR，9883，入口 `POST /asr?sr=16000`，body 裸 PCM16LE |
| 唤醒模型（可选） | openWakeWord 的 `.onnx`，自行训练 |
| 参考音（必需） | 3~10 秒干声 + **逐字对应**的文本文件 |

## 1. 起 VAD（9884）

```bash
python vad/vad_server.py          # 需要 funasr + torch；首次会下 fsmn-vad 模型
curl http://127.0.0.1:9884/health
```

## 2. 起语音入口（8780）

```bash
export VOICE_BRAIN=http://192.168.137.10:8777
export VOICE_REF=/path/to/ref.wav
export VOICE_PROMPT=/path/to/ref.txt
python edge/voice_edge.py
```

`/health` 里 `tts:false` 就是参考音没配对（TTS 会 502），`brain:false` 是对面没起来或地址不对 ——
**这两个字段别当成装饰，看门狗就是靠它们判死的**。

## 3. 起中转（8778，手机聊天页走这条）

```bash
python edge/relay.py --listen 8778 --target 192.168.137.10:8777
# 或者让它自己找（对面在 ICS/热点后面、IP 每次开机都变时）：
RELAY_TARGET= python edge/relay.py --listen 8778
```

> 中转是**裸 TCP 转发**，没有 `/health`。探活只能用 TCP connect ——
> 用 `curl 8778/health` 会把对面服务的状态当成中转的状态，对面一停就误判。

## 4. 起信箱（8790，两台机离线互传）

```bash
export MAIL_KEY=<两台机约定的同一串>
python hub/mailbox.py
curl -H "X-Mail-Key: $MAIL_KEY" http://127.0.0.1:8790/mail/health
```

## 5. 开机自启 + 崩溃拉起（Windows）

```powershell
schtasks /create /tn "Yaya Voice Hub"      /tr "<venv>\pythonw.exe -u <repo>\hub\hub.py" /sc onstart /ru SYSTEM /rl HIGHEST /f
schtasks /create /tn "Yaya Voice Hub Watch" /tr "<venv>\pythonw.exe -u <repo>\hub\hub.py" /sc minute /mo 2 /ru SYSTEM /rl HIGHEST /f
```

要点：

- **SYSTEM 身份**跑，否则电脑锁屏/切换用户就断。
- 计划任务默认「用电池时不启动」要关掉，否则笔记本拔电就停。
- 服务变成 SYSTEM 之后，**普通权限杀不掉**：改 `<repo>/hub/restart.txt` 写服务名，让看门狗来杀。
- 冷启动顺序：TTS/VAD/ASR/中转 并行起，`edge` 等它们真就绪再起（否则 8780 会在 TTS 没好时对外报绿）。

## 6. 防火墙

```powershell
# 语音入口与信箱都要放行，且只放行内网段
New-NetFirewallRule -DisplayName "Voice Edge 8780" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8780 -RemoteAddress 192.168.31.0/24
New-NetFirewallRule -DisplayName "Voice Mail 8790" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8790 -RemoteAddress 192.168.31.0/24
```

`New-NetFirewallRule` 需要管理员。非管理员会话里会报 `拒绝访问`，
用 `Start-Process powershell -Verb RunAs` 提权，**脚本自己写日志文件**（提权窗口的 stdout 拿不到）。

## 7. 验收清单

```bash
curl -s http://127.0.0.1:8780/health   # ok/vad/asr/tts/filler/brain 全 true
curl -s http://127.0.0.1:8790/mail/health
curl -s "http://127.0.0.1:8780/tts?text=%E4%BD%A0%E5%A5%BD" -o /tmp/a.wav && ls -l /tmp/a.wav
```

- `/tts` 返回 0 字节 → 参考音路径不对或文本与音频不对应
- `/tts` 返回 502 → 9880 没起，或参考音不在服务端本地
- `/health` 的 `brain:false` 但手机能通 → 对面地址变了，看 `brain_url` 字段
- 手机能唤但收不到回复 → 先看 `edge` 日志里有没有 `turn start`，再看到底是「没判出 end」还是「大脑没回」
