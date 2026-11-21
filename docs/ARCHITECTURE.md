# 架构与实现说明

## 进程模型

单进程、单 asyncio 事件循环：

```
asyncio loop
├── SIP UDP endpoint (5060)        sip/server.py
├── RTP UDP endpoints (动态端口)    rtp.py / media.py
├── 媒体桥任务（每 20ms 一次）       media.py::Bridge
├── modem 轮询任务（20s）           ec20/modem.py
├── 通话状态轮询（1s，仅通话中）      ec20/modem.py::_call_poll
├── 音频读写任务（arecord/aplay）    ec20/audio.py
└── Web 线程（ThreadingHTTPServer）  web/server.py
        ↕ run_coroutine_threadsafe
```

只有一个线程碰 SQLite（`Db` 内部加锁），Web 线程需要改状态时用
`asyncio.run_coroutine_threadsafe(...).result()` 调度回 loop。

## 呼叫模型：全部走 B2BUA

不做 proxy 转发，任何一通电话都是"两条腿"：

| 场景 | leg A | leg B |
| --- | --- | --- |
| 分机 ↔ 分机 | UAS（被叫方发起） | UAC（我们打给被叫） |
| 分机 → 外线 | UAS | 无 SIP 腿，接 EC20 通话 |
| 外线 → 分机 | UAC（群振多个） | 无 SIP 腿，接 EC20 通话 |
| 面板拨号 | 无 | 无（仅 EC20 通话与音频） |

好处：NAT 只面对本机、RTP 永远经过我们（便于录音/转码）、不需要 Route/Record-Route 状态机。
代价：CPU 略高，但 8kHz 单声道 G.711 的转码量对 4 核 A53 可以忽略。

## 媒体路径

```
话机 ──RTP(PCMU)──► RtpPort ──decode──► 抖动队列 ──┐
                                                   ├─ Bridge（20ms tick） ─► ...
EC20 ──USB PCM──► ModemPort ──capture──► 抖动队列 ──┘
```

- 抖动缓冲 6 帧（120ms），超出丢最旧帧，内存恒定。
- 每个 tick 从两侧各取 1 帧（取不到补静音），编码后发给对侧。
- 时钟用 `time.monotonic()` 做绝对对齐，落后超过 200ms 时重新对齐，避免累积漂移。
- DTMF：SIP 侧收 RFC2833 → 转 `AT+VTS="x"`；反向未实现（EC20 不上报远端 DTMF）。

## 状态与持久化

| 数据 | 位置 |
| --- | --- |
| 分机、短信、规则、设置、CDR、转发日志 | `/var/lib/ec20-sip/ec20sip.db`（WAL） |
| 注册关系 | 同库 `registrations` 表（重启后仍可用于回拨，过期自动清理） |
| 启动参数（端口、设备节点、路径） | `/etc/ec20-sip/config.json` |
| 运行日志 | `/var/log/ec20-sip.log`（1MB × 3 轮转）+ 内存环形缓冲（面板「日志」页） |

## SIP 行为细节

- `REGISTER`：无 Authorization → 401 + `WWW-Authenticate`；校验通过 → 200 + `Contact`/`Expires`；
  `Expires: 0` → 注销。Contact 里 host 为 `0.0.0.0` 时用 UDP 源地址。
- `INVITE` 来自未登记分机 → 403；目标分机未注册 → 480。
- 2xx 后按 500ms/1s/2s/4s 重传 200 OK 直到收到 ACK（丢 ACK 时避免话机单通）。
- 我们发起的 INVITE 同样有 500ms/1s/2s/4s 重传，收到 1xx 停止。
- 群振：最多 8 个分机同时 INVITE，第一个 200 OK 胜出，其余发 CANCEL。
- `MESSAGE`：Request-URI 的 user 当被叫号码，正文走 `AT+CMGS`，成功回 202。
- `INFO`：解析 `Signal=` / 单字符，转 DTMF 给 EC20。
- 未知方法 → 501；无法匹配对话的 BYE/CANCEL → 481。

## 安全边界（当前实现）

- 分机认证：MD5 摘要（SIP 标准），密码存 SQLite 明文（面板需要显示给话机配置）。
- INVITE 只校验"From 是已知且启用的分机"，不强制二次摘要 —— 兼容性优先。
  需要更严时可在 `CallManager.on_invite` 前加 `DigestAuth.verify`。
- Web 面板：PBKDF2-SHA256（12 万轮）+ HttpOnly 会话 Cookie + 24h 过期；无 HTTPS。
- 建议只在局域网使用；要暴露公网请加防火墙白名单或反代 TLS。

## 为什么不用 Asterisk / Kamailio / Docker

| 方案 | 空闲 RSS | 说明 |
| --- | --- | --- |
| Asterisk + chan_quectel | 80–120MB | 需编译模块、dialplan、AMI，功能过剩 |
| Kamailio | 25–35MB | 只做信令，媒体还要 rtpengine（另 40MB+） |
| Docker 化 | +60MB | containerd + shim + 镜像层 |
| **本项目** | **13MB** | 信令 + 媒体 + 短信 + 面板一体 |

在只有 ~590MB 可用内存、还要跑 alist/aria2/filebrowser 的盒子上，这个差别是决定性的。
