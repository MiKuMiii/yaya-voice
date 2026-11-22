# 怎么参与

这个仓库由**两台机器共同维护**：台式主机跑边缘侧（音频），笔记本跑大脑侧（人格/记忆）。
改任何东西之前先记住一句话：**这份代码是给两台不同的机器同时跑的**，单边改完就算没做完。

---

## 一、铁律：推之前必须过 sanitize

```bash
python sanitize.py .
```

输出必须是这一句：

```
干净：没有禁入二进制，也没扫到 key / 内网 IP / tailnet / 本机路径 / QQ 号。
```

**不是这一句就不许推。** 没有例外，尤其是下面三类：

| 类别 | 例子 | 为什么 |
|---|---|---|
| 凭据 | 信箱口令、API key、token、房间号 | 一旦公开，等于把门钥匙贴门口 |
| 具体内网地址 | `192.168.31.<主机号>`、`192.168.137.<主机号>` | 网段 `/24` 可以留作示例，具体主机不行 |
| 本机绝对路径 | `D:\GPT-SoVITS\...`、`C:\Users\<你>\...` | 换台机器就废，且暴露目录结构 |

### 对 `brain/` 的额外要求

大脑侧那台机器上有**不止语音**的东西（米家、信箱、房间、游戏都在同一个进程里）。
往这个仓库推 `brain/` 时，只允许抽**语音路由那一段**，并且：

1. 推之前先本地跑一次 `sanitize.py`，把输出贴进 PR / commit message；
2. 不许带任何设备 token、房间 id、家庭成员称呼、真实 history 样本；
3. 人格提示词只放**结构**（怎么拼、多长、什么时候注入），不放真实人格正文和真实记忆。

一句话：**这个仓库存的是「怎么做」，不是「她是谁」。**

---

## 二、自动化：装一下 pre-commit 钩子

```bash
git config core.hooksPath .githooks
```

装完之后每次 `git commit` 都会自动跑 `sanitize.py`，不干净直接拒绝提交。
（钩子在 `.githooks/pre-commit`，纯 shell，Windows 下由 Git 自带的 sh 执行。）

想临时跳过（不推荐）：

```bash
git commit --no-verify
```

---

## 三、改协议怎么走

协议文件是 `docs/protocol.md`，**两边都按它实现**，所以：

1. 改字段 → 先改 `docs/protocol.md`，写清「旧行为 / 新行为 / 什么时候切换」；
2. 同一轮里把 `edge/`、`client/`、`brain/` 三边都改到能跑；
3. commit message 里写清版本号（`docs/protocol v0.3: ...`）；
4. 单边改完不许推 `main` —— 宁可留在分支上挂着。

历史版本留在 `docs/` 里，别删：`v0.1` 的草稿和 `v0.2` 的差异表都还有用。

## 四、提交信息

```
<范围>: <做了什么>
```

范围取 `edge` / `brain` / `client` / `vad` / `hub` / `docs` / `config`，一行说清，正文写**为什么**（不是写「改了 X」，是写「因为实测 Y，所以改成 Z」）。

例：

```
edge: 悔棋后 2s 无新声就按已说的收尾

实测长停顿录音会被判两次 end、两次 undo，最后靠 9884 的 6s 整轮超时收尾，
端到端白等 4 秒。加一个 resume_id 编号的兜底定时器，有人声就作废它。
```

## 五、不许进仓的东西

- 模型权重（`.pth` / `.ckpt` / `.onnx`）、参考音、任何训练数据
- 录到的真实语音、日志、dump 的 wav
- 编译产物（`.apk` / `.zip` / `.exe`）
- 带真实地址和口令的配置文件（只提交 `*.example.json`）

`.gitignore` 已经拦了大部分，`sanitize.py` 会再拦一遍二进制。**两道闸，别指望其中一道。**

## 六、当前环境的一个坑

这台机器到 GitHub 的 443 直连会被 reset，只能用 **SSH over 443**：

```bash
git remote set-url origin ssh://git@ssh.github.com:443/MiKuMiii/yaya-voice.git
git config core.sshCommand "ssh -i <你的密钥> -o IdentitiesOnly=yes -o StrictHostKeyChecking=no"
```

配好之后 `git push` 和平时一样。细节和另一个坑（`GIT_SSH_COMMAND` 里反斜杠会被吃掉）见
`docs/PITFALLS.md` 第 38 条。
