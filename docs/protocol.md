# 双端语音链路 · 协议 v0.2

一条链路两半：**边缘侧**（有麦克风和音箱的那台，负责听和说）与**大脑侧**（跑模型的那台，负责想）。
两边只靠下面这几个 HTTP 接口说话，谁也不许私自加字段。

> v0.2 = 邻居的 v0.1 设计稿 + 边缘侧现网实现对齐。**差异清单见 §8**，每一条都标了定案方向。

## 0. 音频约定

- 上行：**PCM16LE、单声道、16000 Hz**，0.5 秒一包 = 8000 样本 = **16000 字节**
- 下行：采样率由边缘侧决定，响应里用 `mime` 说明（本实现实测 32000 Hz 更自然）
- 手机上行的 base64 放在 JSON 的 `pcm` 字段

## 1. 手机/客户端 → 边缘侧：`POST /voice_edge`

```json
{"code":"<访问口令>","sid":"<会话id>","pcm":"<base64 PCM16LE>","force":false,"filler":true,"playing":false}
```

| 字段 | 含义 |
|---|---|
| `code` | 口令，服务端配置了才校验 |
| `sid` | 客户端生成，建议 `apk-<随机>`；一轮一个 sid，可复用 |
| `force` | `true` = 客户端已判唤醒，边缘侧跳过自己的唤醒判定直接开录 |
| `filler` | `true` = 允许先回垫音 |
| `playing` | `true` = 客户端正在放音；边缘侧丢弃此包并清 VAD 缓冲 |

响应（JSON，事件式）：

```json
{"event":"wake|recording|state|filler|reply|more|end|idle|stop|error",
 "sid":"…","state":"idle|listening|thinking|speaking",
 "text":"…","mime":"audio/wav","audio_b64":"…","sr":32000,
 "part":0,"budget_ms":4000,"i":7,"next":8,
 "reason":"silence|timeout|maxlen|end_cmd","turn_ms":6320}
```

| event | 含义 |
|---|---|
| `idle` | 没在听（未唤醒/已回落），客户端保持静默 |
| `wake` | 这一包命中了唤醒 |
| `recording` | 正在收音，客户端不必有动作 |
| `state` | 只带 `state`（如 `thinking`），无音频 |
| `filler` | 垫音（预生成的一句），先出声占位 |
| `reply` | 首段正文音频（`text` 在这一段才有） |
| `more` | 后续分段音频（`text` 只出现在同一句的第一段） |
| `end` | 本轮结束，`reason` 说明判定依据 |

**硬性约束**：一轮只有一次 `reply`、若干 `more`、一次 `end`。

`end` 之后同一 `sid` 的新音频属于下一轮。这里有两个**互不相干**的行为，别混成一条：

| 行为 | 在哪一侧 | 值 | 作用 |
|---|---|---|---|
| `end` 后的迟到包直接丢 | **枢纽（边缘侧）** | 0.35 s | 上一轮的尾巴，别让它污染新一轮 |
| 播完后不认唤醒的冷却 | **大脑/客户端侧** | 1.5 s | 避免自己刚说的话触发自己的唤醒 |

## 2. 取回复段：`POST /api/voice/reply`

```json
{"code":"可选","token":"<sid>:<序号>"}
```

| 返回 | 含义 |
|---|---|
| `{"ok":true,"payload":{…}}` | 拿到一段；`payload.i` 是它的序号，下次用 `payload.next` |
| `{"ok":true,"waiting":true}` | 还没有新段，400ms 后再来 |
| `{"ok":true,"lost":true,"next":N}` | 序号落后（队列已推进），从 `next` 重新取 |

> `next` 是**整数事件序号**，不是令牌字符串。v0.1 设计稿里写的是 `event:"lost"` + `next:"<token>"`，
> 以本节为准（2026-11-22 双方确认）。

**轮询礼仪**：建议**常开轮询**（400ms 一次），不要等收到 `filler` 才开始——
现网实测这一项决定垫音是 700~800ms 还是 1.8s。

> `GET /voice_edge/reply?sid=&wait=3000` 是 v0.1 的长轮询写法，本实现在收到事件后
> 仍会触发一次，但**不要依赖它**：序号语义以本节为准。

## 3. 收尾 / 打断

```
POST /voice_edge/end?sid=<sid>     无 body
→ 200 {"ok":true,"event":"end","sid":"…"}     // 无此会话也回 200，附 note
```

- 语义：停 TTS 队列（本轮 `turn_seq` 自增，正在生成的段立即作废）、清 VAD、回 idle。
- 送大脑**之前**还有一层结束词判定：强口令（退下/退一下/关掉/关闭语音/别说了/收工…）
  出现即收尾；弱口令（结束/先这样/再见/晚安…）要求去掉唤醒词后 ≤6 字。
  命中则不送大脑、不生成音频。

## 4. 边缘侧 → 大脑侧

### 4.1 一次性：`GET|POST /api/voice/brain?text=&sid=`

```json
{"ok":true,"text":"…","ms":1234,"hist":2,"silent":false}
```

- `text` 已剥离唤醒词；整句只有唤醒词时返回 `{"ok":true,"text":"","skip":"wake-only"}`
- `silent=true` 表示「夜里只回文字，别出声」（**v0.2 待实现**，见 §8）

### 4.2 流式：`GET /api/voice/brain/stream?text=&sid=`（SSE）

```
data: {"i":0,"text":"嗯，在呢。"}
data: {"i":1,"text":"今天想聊点什么？"}
data: {"done":true,"chunks":2,"first_ms":1168}
```

- 服务端按句切：句号/问号/叹号/省略号且 ≥4 字就切；否则 ≥16 字时在逗号/顿号/分号处切；
  都没有就切 16 字；不足 6 字不切。
- 出错用 `data: {"done":true,"error":"…"}` 收尾，客户端当正常结束处理。
- **一轮只调一次**：这个接口会写历史，重试/并发会造成重复记录。
- 超时：连接 4s / 首字节 6s / 整轮 30s。

## 5. 第三方调用 TTS：`GET|POST /tts`

```
GET  /tts?text=<utf8 urlencode>
POST /tts  {"text":"…"}    或表单
```

- 返回 `audio/wav`（32kHz 单声道 16bit）。
- **未知参数一律忽略**（`text_lang`/`media_type`/`ref_audio_path`/`prompt_text`/`prompt_lang`/
  `speed_factor`/`top_k`/`top_p`/`temperature`/`seed` 都可带，不会 422）。
- `ref_audio_path` 指向**服务端真实存在**的文件才采用，否则静默回落默认参考音。
- 文本上限 600 字；空文本 `400 {"ok":false,"error":"empty text"}`；合成失败 `502`（调用方回落纯文本）。
- 并发建议 ≤2（单卡 TTS，多了互相抢 GPU）。

## 6. 状态机（客户端只渲染、不判定）

```
idle --force/wake--> listening --end(有文本)--> thinking --> speaking --> idle
speaking --口令(退下/别说了)--> listening
任何状态 --end 指令--> idle
```

服务端实现细节：

- 判完 `end` 后有 **悔棋窗口**（默认 700ms，`VOICE_GRACE` 可调）：期间又出现语音能量就作废这次 end，继续听。
- `playing:true` 期间所有包直接丢，并清 VAD 缓冲（防止把自己的声音当输入）。
- 唤醒后若整轮只剩唤醒词，**不送大脑**，回 `recording` 继续听。

## 7. 超时与降级

| 环节 | 超时 | 行为 |
|---|---|---|
| 单包上行 | 3 s | 丢包，不阻塞后续 |
| 单包处理 | 目标 <80ms | 实测 30~68ms |
| 唤醒（含 ASR 复核） | <400ms | 实测 155~307ms |
| 末包 → 垫音 | <1.2s | 实测 0.7~1.8s（取决于客户端是否常开轮询） |
| 垫音 → 首段音频 | <2s | 实测 0.5~1.7s（大脑首字占 1.2~1.5s） |
| 整轮 | 30 s | 强制 end，避免客户端吊死 |
| 大脑不可达 | 连接 4 s | 直接回 end，客户端提示「我这边没接上」 |

## 8. v0.1 与实现的差异（定案）

| # | 议题 | v0.1 写法 | 现网实现 | 定案 |
|---|---|---|---|---|
| 1 | 取段接口 | `GET /voice_edge/reply?sid=&wait=` | `POST /api/voice/reply` + `token=sid:序号` | **以 POST 为准**，GET 保留兼容 |
| 2 | 续传令牌 | 响应里带 `more:"<下一个token>"` | `i` / `next` 两个整数 | **用 `i`/`next`**；`more` 仅作事件名 |
| 3 | 垫音时机 | 「立刻返回，0.1 秒内」 | 悔棋窗口 700ms 后延后发 | 保留 700ms 窗口（否则半句停顿会被截断） |
| 4 | `budget_ms` / `part` / `reason` / `turn_ms` | 无 | 有 | **纳入协议**（客户端可据此抢断、分句渲染、排查） |
| 5 | `silent`（夜里只回文字） | 大脑返回 | 边缘侧未实现 | **v0.2 待实现**：拿 `silent` 就不合成音频，只回 `text` |
| 6 | 错误三态 | 明确要求 | 已遵守 | 保留（见 §9） |

## 9. 错误与三态

所有接口必须显式表达三种结果：**成功 / 失败 / 说不清**，禁止静默成功。

- `200 + ok:true` = 成了
- `200 + ok:false + error` = 明确失败
- 超时 / 连不上 = 说不清，客户端按「先保命」处理（回 idle，不重放）

## 10. 不在这份协议里的东西

- AEC / 降噪：必须在同机做，跨机传参考信号会错位几十毫秒，抵消不了。
- 唤醒词模型、人格、记忆、声纹：各留各那侧，不走协议。
- 模型权重与音色：见 `MODELS.md` / `CREDITS.md`，不进仓库。
