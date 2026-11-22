# VoiceLink Lite（手机端最小参考实现）

这套东西只做一件事：**把麦克风的声音按协议喂给边缘侧，把它回来的音频放出来**。
判唤醒、转写、说话停没停、合成语音、记历史——全在边缘侧做。手机端不猜、不判断、不存。

## 不是什么

- 不是完整 App。没有聊天页、没有小菜园/小屋、没有四态悬浮球动画、没有声纹。
- 不含任何密钥、地址、模型权重。所有配置在界面上填，存在 `SharedPreferences`。
- 没做 AEC/降噪的特殊处理（用系统 `VOICE_RECOGNITION` 源，靠系统自带的回声消除）。

## 组成（四个文件，约 500 行）

| 文件 | 干什么 |
| --- | --- |
| `MainActivity.java` | 两个输入框（地址、口令）+ 开始/停止 + 状态与最后一句 |
| `VoiceService.java` | 前台服务：录音 → 0.5 秒一包 POST → 收事件 → 放音频 |
| `Config.java` | 读写地址与口令 |
| `AndroidManifest.xml` | 录音 / 网络 / 前台服务（microphone 类型）|

## 与边缘侧的一轮交互

```
手机                                边缘侧（8780）
 |-- POST /voice_edge {code,sid,pcm,force,filler,playing} -->|
 |<-- {event:"wake"} / {event:"recording"} ------------------|
 |<-- {event:"filler", token, audio_b64} --------------------|   先垫一句，别让静音太久
 |-- GET /voice_edge/reply?sid=&wait=3000 ------------------>|
 |<-- {event:"reply", text, audio_b64, more:"<下一个token>"} -|
 |<-- {event:"more", ...} … {event:"end"} -------------------|
 |-- GET /voice_edge/end?sid=  （用户挂断时）---------------->|
```

- 音频一律 `PCM16LE / 16000 Hz / 单声道`，每包 0.5 秒（8000 样本）。
- 上行 base64 放在 JSON 的 `pcm` 字段里。
- `more` 是"还有下一段"的令牌，顺着它继续拉；`end` 表示这一轮说完了。
- 播报期间 `playing=true` 且**不外发音频**，避免把自己的声音喂回去。

## 编译

只依赖 Android SDK（build-tools 34 + platform 34）和 JDK 17，不需要 Gradle：

```bash
aapt2 compile --dir res -o build/res.zip
aapt2 link -o build/app-unsigned.apk -I $ANDROID_JAR --manifest AndroidManifest.xml \
    --min-sdk-version 26 --target-sdk-version 34 -R build/res.zip --java build/gen
javac -encoding UTF-8 -source 8 -target 8 -bootclasspath $ANDROID_JAR -d build/classes $(find src build/gen -name '*.java')
d8 --min-api 26 --lib $ANDROID_JAR --output build/dex build/classes/**/*.class
zip -j build/app-unsigned.apk build/dex/classes.dex
zipalign -f 4 build/app-unsigned.apk build/app-aligned.apk
apksigner sign --ks <你的.jks> --out voicelink.apk build/app-aligned.apk
```

（`ant`/Gradle 都行，只要最后把 `classes.dex` 塞进 APK。）

## 跑起来

1. 装到手机上，打开，填边缘侧地址（形如 `http://192.168.x.x:8780`）和口令；
2. 点「开始通话」，给录音权限，保持前台通知；
3. 对手机说唤醒词（默认「雅雅」），然后正常说话；
4. 说「退下」或点「停止」收摊。

## 已知的坑

- 安卓 12+ 起，后台长时间录音必须带 `foregroundServiceType="microphone"`，否则被系统掐。
- 中文路径下 `aapt2` 会报 `failed to open directory`，用 ASCII 目录或做 junction。
- `AudioTrack` 的采样率要按 wav 头来（边缘侧可能给 32k），写死 16k 会变调。
- 播放和录音共用设备，不做 AEC 会把自己的声音当输入。
