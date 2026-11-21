# FRP 穿透配置（SIP over TCP + RTP over UDP）

本项目 SIP **只监听 TCP**，RTP 仍是 UDP。这个组合是被 frp 的语义逼出来的，也是它的最优解：

| 用途 | 隧道类型 | 端口 | 原因 |
| --- | --- | --- | --- |
| SIP 信令 | **tcp** | 5060 | TCP 是长连接，一条隧道可承载任意多个分机；服务端能沿连接反向发 INVITE/BYE |
| RTP 媒体 | **udp** | 10000/10001/10003/10004 | RTP 协议本身是 UDP，`type="tcp"` 隧道转发不了 |

## 一、为什么 SIP 必须 TCP（一对一多）

frp 的 `type="udp"` 是单向的：只有外部客户端先发包才能建立会话。实测（UDP 版）：

```
register 1003 -> 200 OK
caller <- 100 Trying
caller <- 180 Ringing
callee received: nothing          <- 服务端主动 INVITE 送不到 NAT 后的分机
```

换成 TCP 后同一条探针（`tools/probe_nat_call.py`，走公网 `8.137.213.214:5060`）：

```
caller <- 100 Trying
caller <- 180 Ringing
callee received: INVITE from <sip:1001@8.137.213.214>;tag=2845d6196fc0
RESULT: server->callee reachable = True
```

所以：**一条 5060/tcp 隧道 = 任意多台分机同时注册 + 互打 + 被外线呼入**。

## 二、必须同步的两处配置

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

- `external_ip`：公网地址，写进 SDP 的 `c=` 行，否则对方把音频发到内网 IP。
  （本项目会按对端来源自动选择：内网分机仍通告内网 IP，不绕公网。）
- `rtp_ports`：RTP 端口池，**每路通话消耗 2 个**（主叫腿 + 被叫腿各一个）。
  4 个端口 = 2 路并发；要 4 路并发就再加 4 个 udp 隧道并扩展这个列表。

改完 `systemctl restart ec20-sip`，验证：

```sh
curl -s http://127.0.0.1:8080/api/status | python3 -c "import json,sys;print(json.load(sys.stdin)['sip'])"
# {'listening': '8.137.213.214:5060', 'rtp': {'ports': [...], 'in_use': [...]}}
ss -tlnp | grep 5060        # TCP
ss -lunp | grep 1000        # UDP，仅通话期间出现
```

## 三、frp 端

用 `packaging/frpc.toml`（5060 一条 tcp + 4 条 udp）。启动与自检：

```sh
frpc verify -c /etc/frp/frpc.toml
systemctl restart frpc
journalctl -u frpc --since '-2min' | grep -c 'start proxy success'   # 应为隧道条数
```

## 四、分机（话机/软电话）配置

必须支持 **SIP over TCP**：

```
服务器: 8.137.213.214
端口:   5060
传输:   TCP          ← 关键，不能选 UDP
账号:   1002
密码:   123456
域名:   ec20sip.local（或 8.137.213.214）
```

不支持 TCP 的老话机：在 `config.json` 里设 `"sip": {"enable_udp": true}` 并重启，
它就能走 UDP（但只能呼出，服务端呼不回来——见第一节）。

## 五、故障对照

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `frpc verify` 报语法错 | 0.65 只认 TOML | 用 `packaging/frpc.toml` |
| 注册成功但无声音 | RTP 用了随机端口 | 配 `sip.rtp_ports` 与隧道一致 |
| 无声音且通话中 RTP 端口没被占用 | `external_ip` 没改成公网地址 | SDP 通告了内网 IP |
| 外部分机能呼出、不能呼入 | 只开了 UDP（`enable_udp=true`） | 话机改 TCP 传输 |
| 第 3 路通话开始失败 | `rtp_ports` 不够（每路 2 个） | 加端口 + 加 udp 隧道 |
| `rtp port 10000 unavailable` | 端口被别的进程占用 | `ss -lunp \| grep 1000` |
| 挂断后对端还在通话 | 老版本（UAS 侧不发 BYE） | 升级到当前版本，已修复 |