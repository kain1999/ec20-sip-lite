# FRP 穿透配置（SIP + RTP）

盒子在运营商/家庭 NAT 后面时，用 frp 把 SIP 与 RTP 暴露到公网服务器。
本项目的 frp 相关文件：

| 文件 | 作用 |
| --- | --- |
| `packaging/frpc.toml` | 5 条 udp 隧道（5060 + 10000/10001/10003/10004） |
| `packaging/frpc.service` | frpc 的 systemd 单元（`MemoryMax=80M`） |
| `/etc/ec20-sip/config.json` → `sip.rtp_ports` | 把 RTP 锁到隧道映射的那几个端口 |

## 一、必须同时改的两处

frp 只映射固定端口，而本项目默认用**系统临时端口**跑 RTP。只配 frp 不改 `rtp_ports`
的结果是：信令通、音频单通（因为你听到对方、对方听不到你）。

```json
// /etc/ec20-sip/config.json
{
  "sip": {
    "port": 5060,
    "external_ip": "8.137.213.214",
    "rtp_ports": [10000, 10001, 10003, 10004]
  }
}
```

`external_ip` 填 frp 服务器的公网地址：SDP 里必须通告这个地址，否则对方会把音频发到
盒子的内网 IP。改完 `systemctl restart ec20-sip`。

验证：

```sh
curl -s http://127.0.0.1:8080/api/status | python3 -c "import json,sys;print(json.load(sys.stdin)['sip'])"
# {"listening": "8.137.213.214:5060", ..., "rtp": {"ports": [10000,10001,10003,10004], "in_use": []}}
```

通话期间 `in_use` 会出现占用的端口，`ss -lunp | grep 1000` 能看到绑定。

## 二、frp UDP 的方向性限制（重要）

frp 的 `type="udp"` 是**反向隧道**：外部客户端先发包 → frps 建立工作连接 → 转发到盒子。
因此：

| 场景 | 是否可用 | 原因 |
| --- | --- | --- |
| 外部分机向盒子 REGISTER | ✅ | 客户端先发包，隧道已建立 |
| 外部分机呼入（打外线、打内部分机） | ✅ | 同上，信令由客户端发起 |
| 通话中的音频（双向） | ✅ | 双方都在持续发包，隧道活跃 |
| **盒子主动呼叫外部分机**（含 EC20 外线来电群振） | ❌ | 服务端要发起全新的 UDP 会话，frpc 侧没有对应 socket |
| 外部分机之间的互打 | ❌ | 需要服务端向被叫发起 INVITE |

实测（`tools/probe_nat_call.py`，走公网 `8.137.213.214:5060`）：

```
register 1003 -> 200 OK (contact host 0.0.0.0)
caller <- 100 Trying
caller <- 180 Ringing
callee received: nothing            <- 服务端无法把 INVITE 送到 NAT 后的分机
RESULT: server->callee reachable = False
```

这不是配置错误，是 UDP 反向代理的固有语义：服务端往 `127.0.0.1:<源端口>` 发的包，
frpc 上没有监听者，直接丢弃。

## 三、要让盒子能呼通远程分机，选一个

### 方案 A：Tailscale / WireGuard（推荐，盒子已装 tailscale）

远程手机加入同一 tailnet，直接访问盒子内网地址 `sip:1003@100.x.x.x:5060`。
SIP 与 RTP 全双向，不需要 frp，也不消耗公网带宽。frp 可以只留 Web 面板隧道。

```sh
# 盒子
tailscale up            # 或用 /opt/tailscale 里的现成配置
tailscale ip -4         # 得到 100.x.y.z，把它填进话机的 SIP 服务器地址
```

### 方案 B：frp 的 tcp 隧道 + SIP over TCP

TCP 有连接可复用，服务端可以沿已建立连接发 INVITE，双向都通。
需要在 frp 服务端新增一条 `type = "tcp"` 隧道，并在本项目启用 TCP 监听
（当前版本只监听 UDP，需要额外开发）。

### 方案 C：只做"呼入"用途

如果远程分机只需要**打出去**（拨外线、拨内部分机），保持现在的 UDP 隧道即可；
但要接受"别人打不进来、外线来电不会振铃到该分机"。

## 四、常见故障对应

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `frpc verify` 报语法错 | 0.65 只认 TOML，旧 ini 语法不行 | 用 `packaging/frpc.toml` |
| 注册成功但拨号无响应 | 隧道名/远端端口在 frps 侧未放行 | 看 `journalctl -u frpc` 有无 `start proxy success` |
| 能听到对方、对方听不到 | RTP 用了随机端口 | 配 `sip.rtp_ports` = 隧道端口列表 |
| 能听到对方、对方听不到（已配 rtp_ports） | `sip.external_ip` 没改成公网地址 | SDP 通告了内网 IP |
| 呼叫外部分机超时 | frp UDP 方向性限制 | 见「三」，换 Tailscale 或 TCP |
| 日志 `rtp port 10000 unavailable` | 端口被别的进程占用 | `ss -lunp \| grep 1000` 查占用者 |