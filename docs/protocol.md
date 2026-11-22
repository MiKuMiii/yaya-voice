# 线协议 v0.1（草稿，未定稿前任何一方不得单方面改字段）

三组接口：**手机 ↔ 语音机**、**语音机 ↔ 大脑机**、**语音机 ↔ 第三方 TTS 调用方**。
所有请求 UTF-8；音频一律 **PCM16LE / 16kHz / 单声道**（TTS 输出例外，见 §4）。

---

## 1. 手机 → 语音机：`POST /voice_edge`

`Content-Type: application/json`

```jsonc
{
  "code":   "可选，口令（配置了才校验）",
  "sid":    "会话 id，客户端生成，建议 apk-<随机>",
  "pcm":    "base64(PCM16LE 16k mono)，常规 0.5s = 16000 字节",
  "force":  false,      // true = 不判唤醒，直接开始一轮（按钮/唤醒词由客户端判定时用）
  "filler": true,       // true = 允许先回垫音
  "playing": false      // true = 手机正在放音，语音机应丢弃这包并清 VAD
}
```

### 返回（一个 JSON）

```jsonc
{
  "event": "idle | wake | recording | state | filler | reply | more | end",
  "sid":   "回显",
  "state": "idle | listening | thinking | speaking",   // 客户端**只按它渲染**，别自己按音量猜
  "text":  "这一段的文本（reply/more 的第一段才有）",
  "audio_b64": "base64(wav)",
  "mime": "audio/wav",
  "sr":   32000,
  "part": 0,                 // 同一句被切成多段时，第几段
  "budget_ms": 4000,         // 这一段建议的播放预算，客户端超时可抢断
  "i": 7, "next": 8,         // 事件序号（配合 §2 的轮询）
  "reason": "silence | timeout | maxlen | end_cmd",   // 仅在 end 时出现
  "turn_ms": 6320            // 本轮累计音频毫秒（不是墙钟）
}
```

事件语义：

| event | 含义 |
|---|---|
| `idle` | 没在听（未唤醒/已回落）。客户端应保持静默 |
| `wake` | 这一包命中了唤醒；随后进入 `recording` |
| `recording` | 正在收音；客户端不必有动作 |
| `state` | 只带 `state`，无音频（如 `thinking`） |
| `filler` | 垫音（预生成，固定一句），先出声占位 |
| `reply` | 首段正文音频 |
| `more` | 后续分段音频 |
| `end` | 本轮结束，`reason` 说明判定依据 |

**硬性约束**：一轮只有一次 `reply`、若干 `more`、一次 `end`；`end` 之后同一 `sid` 的新音频
属于下一轮（服务端在 0.35s 内的包视作上一轮尾巴，丢弃）。

---

## 2. 手机 → 语音机：`POST /api/voice/reply`（对齐 AstrBot 风格的取段）

```jsonc
{ "code": "可选", "token": "<sid>:<序号>" }
```

| 返回 | 含义 |
|---|---|
| `{"ok":true,"payload":{…}}` | 拿到一段，`payload.i` 是它的序号，下次用 `payload.next` |
| `{"ok":true,"waiting":true}` | 还没有新段，请 400ms 后再来 |
| `{"ok":true,"lost":true,"next":N}` | 序号落后了（队列已推进），从 `next` 重新取 |

**轮询协议**：收到 `filler` 后开始每 400ms 轮询，取到一段播一段，直到 `end`。
建议**常开轮询**（不等 `filler` 才开），可省掉「等下一个上行包」的 ~1.1s。

---

## 3. 语音机 → 大脑机：`GET /api/voice/brain/stream`（SSE）

```
GET /api/voice/brain/stream?text=<本轮全文>&sid=<会话 id>
Accept: text/event-stream

data: {"i":0,"text":"嗯，在呢。"}
data: {"i":1,"text":"今天想聊点什么？"}
data: {"done":true,"chunks":2,"first_ms":1168}
```

- 服务端**按句切**（过长按逗号，最短 6 字），`i` 从 0 递增。
- 出错用 `data: {"done":true,"error":"..."}` 收尾，客户端当正常结束处理。
- **一轮只调一次**。这个接口会写历史，重试/并发会造成重复记录。
- 超时建议：连接 4s / 首字节 6s / 整轮 30s。

非流式调试接口：`GET|POST /api/voice/brain?text=…` → `{"ok":true,"ms":1318,"text":"…"}`。

---

## 4. 第三方 → 语音机：`GET|POST /tts`（给聊天机器人当 TTS provider）

```
GET  /tts?text=<utf-8 urlencode>
POST /tts   {"text": "..."}      或表单
```

- 返回 `audio/wav`（32kHz 单声道 16bit）。
- **未知参数一律忽略**（`text_lang`/`media_type`/`ref_audio_path`/`prompt_text`/`prompt_lang`/
  `speed_factor`/`top_k`/`top_p`/`temperature`/`seed` 都可以带，不会 422）。
- `ref_audio_path` 若指向**服务端真实存在**的文件则采用，否则静默回落默认参考音。
- 文本上限 600 字；空文本 `400 {"ok":false,"error":"empty text"}`；合成失败回 `502`（调用方据此回落纯文本）。
- 并发建议 ≤2（单卡 TTS，多了互相抢 GPU）。

---

## 5. 收尾 / 打断

```
POST /voice_edge/end?sid=<sid>      无 body
→ 200 {"ok":true,"event":"end","sid":"…"}          // 无此会话也回 200，带 note
```

- 语义：停 TTS 队列（本轮 `turn_seq` 自增，正在生成的段立即作废）、清 VAD、回 idle。
- 送大脑**之前**还有一层结束词判定：强口令（退下/退一下/关掉/关闭语音/别说了/收工…）出现即收尾；
  弱口令（结束/先这样/再见/晚安…）要求去掉唤醒词后 ≤6 字。命中则不送大脑、不生成音频。

---

## 6. 时间预算（参考实现实测）

| 环节 | 目标 | 实测 |
|---|---|---|
| 单包上行处理 | <80ms | 30~68ms |
| 唤醒（含 ASR 复核） | <400ms | 155~307ms |
| 末包 → 垫音 | <1s | 0.7~1.8s（取决于客户端是否常开轮询） |
| 垫音 → 首段正文音频 | <2s | 0.5~1.7s |
| 大脑首字 | 1.2~1.5s | 由大脑侧决定 |

---

## 7. 状态机（服务端权威）

```
idle ──(wake│force)──▶ recording ──(end 且有文本)──▶ thinking ──(首段音频)──▶ speaking
  ▲                        │                                                   │
  │                        └─(end 但只有唤醒词)──────────────┐                 │
  └──(20s 无输入)──────────────────────────────────────────────────────────────┘
```

- 判完 `end` 后有 **悔棋窗口**（默认 700ms）：期间又出现语音能量就作废这次 end，继续听。
- `playing:true` 期间所有包直接丢，并清 VAD 缓冲（防止把自己的声音当输入）。
