# ec20-sip-lite

[![ci](https://github.com/kain1999/ec20-sip-lite/actions/workflows/ci.yml/badge.svg)](https://github.com/kain1999/ec20-sip-lite/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/kain1999/ec20-sip-lite?label=release)](https://github.com/kain1999/ec20-sip-lite/releases)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)

克隆与安装：

```sh
git clone https://github.com/kain1999/ec20-sip-lite.git
cd ec20-sip-lite
sudo sh install.sh            # 装到 /opt/ec20-sip-lite，注册 systemd 服务
```

极低内存的 **SIP 服务器 + Quectel EC20 4G 模块网关**，为 hi3798mv300（hinas / 海纳思）这类
**可用内存不到 500MB** 的盒子上跑而写。单进程 Python 3（仅标准库），实测常驻 **13–23MB**，
不装 Docker、不装 Asterisk、不装 Kamailio、不装 pjsip。

```
 SIP 话机 / 软电话 (Zoiper, MicroSIP, 亿联, 话机…)
        │  REGISTER / INVITE / MESSAGE   (TCP 5060)
        ▼
 ┌────────────────────────────── ec20-sip-lite ──────────────────────────────┐
 │  SIP 注册器 + B2BUA  ──►  媒体桥（20ms PCM / G.711）  ──►  EC20 USB 声卡     │
 │  Web 管理面板 (8080)   ──►  AT 通道 (/dev/ec20-at)    ──►  语音 / 短信 / SIM │
 └───────────────────────────────────────────────────────────────────────────┘
        │  USB (5 × vendor + UAC)
        ▼
   Quectel EC20-CE / EC25  ──►  运营商网络（VoLTE 语音 + SMS + 流量）
```

## 功能

| 模块 | 说明 |
| --- | --- |
| SIP 服务器 | **TCP 注册器 + B2BUA**（默认，可开 UDP）；摘要认证（MD5）、对称 RTP、分机互打、拨外线、外线来电群振、振铃组 |
| 分机管理 | Web 增删改查、随机密码、在线状态、踢下线、呼叫转移字段 |
| EC20 驱动 | 纯 stdlib 的 termios AT 通道、URC 解析、`ATD/ATA/ATH/VTS`、`CMGR/CMGS` PDU 收发、`QPCMV` UAC 开关 |
| 语音 | G.711 PCMU/PCMA 纯 Python 编解码，`arecord/aplay` 抓放 USB 音频，RFC2833 DTMF 双向 |
| 短信 | PDU 编解码（UCS2 中文 / GSM7 / 长短信拼接）、收发存 SQLite、SIP MESSAGE 互转 |
| 转发 | 关键词/号码/全部 → 邮件(SMTP) / 钉钉机器人(加签) / 通用 Webhook，带规则与发送日志 |
| 面板 | 概览、分机、短信、转发规则、设置、日志；深色、移动端可用、无 CDN 依赖 |
| 运维 | systemd 单元（MemoryHigh/Max）、udev 规则、一键安装、一键诊断脚本 |

## 内存预算（实测）

| 组件 | RSS |
| --- | --- |
| Python 解释器 + 本服务（无通话） | 13.3 MB |
| 建立 1 路 SIP 通话（RTP + 桥 + 2 路 G.711） | +2~4 MB |
| 每路额外并发通话 | +1~2 MB |
| `arecord` / `aplay`（仅通话期间） | 2 × ~2 MB |
| **典型峰值（2 路并发）** | **< 30 MB** |

对比：Asterisk + chan_quectel + Kamailio 方案仅 Asterisk 常驻就 80–120MB，Kamailio 再加 30MB，
容器化后还有 containerd/shim 的 ~60MB —— 这正是本项目的由来。

## 安装（在盒子上）

```sh
# 1. 上传/解压到任意目录后
cd ec20-sip-lite
sudo sh install.sh

# 2. 若模块还没被识别（没有 /dev/ttyUSB*）
sudo /usr/local/sbin/ec20-bind.sh

# 3. 一次性打开 USB 音频类 + IMS（会重启模块，AT 口断开约 20 秒）
sudo /usr/local/sbin/ec20-prepare.sh
sudo /usr/local/sbin/ec20-bind.sh

# 4. 打开面板 http://<盒子IP>:8080 ，首次启动日志里有随机管理密码
journalctl -u ec20-sip -n 50 | grep 管理面板
```

安装脚本会自动：装 `alsa-utils`（若缺）、写 `/etc/ec20-sip/config.json`、安装 udev 规则与
`ec20-bind.sh`、创建 `/var/lib/ec20-sip/ec20sip.db`、注册并启动 `ec20-sip.service`。

## 分机用法

1. 面板「分机」页新增，例如 `1001`，密码可留空自动生成。
2. 话机注册地址：`sip:1001@<盒子IP>`，端口 `5060`，**传输选 TCP**（不支持 TCP 的老话机：把
   `sip.enable_udp` 设成 `true` 后重启，它就能走 UDP，但只能呼出、不能被叫），认证用户名/密码同上。
3. 互打：直接拨对方分机号。
4. 拨外线：`9` + 手机号（前缀可在「设置 → 拨号」改），例：`913800138000`。
5. 外线来电：所有在线分机同时振铃，第一个接听者接通，其余自动 CANCEL。
6. 短信：面板收发；也可用 SIP MESSAGE（`sip:13800138000@<盒子IP>`）直接发送。

## 短信转发

「转发规则」里建规则：匹配方式（全部 / 号码包含 / 关键词包含）+ 渠道（email / dingtalk / webhook）+ 模板
（`{peer} {text} {time}` 占位符）。渠道凭据在「设置」里配置并点「测试」。
每条转发结果写入 `forward_log` 表，面板短信条目会显示「已转发」标记。

## 公网穿透（frp）

盒子在 NAT 后面时用 frp 暴露；**必须同时配 `sip.rtp_ports`**，否则音频单通：

```json
{"sip": {"external_ip": "8.137.213.214", "rtp_ports": [10000, 10001, 10003, 10004]}}
```

详见 [docs/FRP.md](docs/FRP.md)。SIP 走 TCP 隧道（一条隧道支持任意多台分机，服务端可沿连接
反向呼出），RTP 走 udp 隧道（RTP 本身就是 UDP）。每路通话占 2 个 RTP 端口，4 个端口 = 2 路并发。

## 分机振铃组（一转多）

分机页「振铃组」填 `1001&1002&1003`（`&` 或逗号分隔）：

- 拨其中任一号，组内所有在线分机**同时振铃**；EC20 外线来电同理
- 第一个接听即接通，其余收到 `CANCEL`；第二台再接听会被 `BYE` 释放，不影响通话方
- 某台拒接/占线不影响其他成员；全组失败才回 486
- 组成员是**对称**的：只在 1001 上写组，拨 1002 也会带起 1001

## Web 登录开关

设置 → 管理账号 → 取消「面板需要登录」即局域网免密访问；开启前必须已设置密码。
只读接口 `GET /api/auth/mode` 可查当前状态。

## 命令行

```sh
python3 -m app.main --sim                 # 无硬件模拟模式（面板、SIP、短信全流程可测）
python3 -m app.main -c /etc/ec20-sip/config.json
python3 -m app.main --print-password      # 重置并打印管理密码
python3 tools/sip_probe.py --server 192.168.1.188:5060 --user 1001 --password xxx --invite 1002
python3 tools/auto_answer.py --server 127.0.0.1:5060 --user 1002 --password xxx --hold 8
python3 tools/webhook_sink.py 18099       # 本地 webhook 接收器，验证短信转发
python3 tools/db_dump.py /var/lib/ec20-sip/ec20sip.db
python3 tools/check_api.py                # 面板调用的接口是否都存在
node tools/check_ui.js                    # 面板 id / action 是否自洽（需 node）
python3 -m unittest discover -s tests -v  # 23 个单元/集成测试
```

模拟模式下额外提供两个调试接口（真实模块运行时不开放）：

```sh
curl -X POST -H 'Content-Type: application/json' -d '{"number":"13800138000"}' \
     -b cookies.txt http://127.0.0.1:8080/api/sim/incoming     # 触发一通来电
curl -X POST -H 'Content-Type: application/json' -d '{"peer":"10001","text":"验证码 123456"}' \
     -b cookies.txt http://127.0.0.1:8080/api/sim/sms          # 触发一条进来的短信
```

## 配置文件

`/etc/ec20-sip/config.json`（示例见 `config/config.example.json`）。改完 `systemctl restart ec20-sip`。
运行期可改的东西（SMTP、钉钉、拨号前缀、SIP realm、密码、短信转发开关）都在面板里，存 SQLite。

## 这台盒子上的实测结论（hi3798mv300 + EC20-CE-HCLG）

| 项目 | 结果 |
| --- | --- |
| USB 串口 | 内核无 `option.ko`，`usbserial_generic` 手工绑定后出现 `/dev/ttyUSB0..5`；AT 口 = 接口 2 |
| `AT+QCFG="usbcfg"` | 出厂 `...,1,1,1,1,1,0,0`（uac=0）→ 改成 uac=1 后出现声卡 `card 2 [EC20CEHCLG]` |
| 声卡 | `pcmC2D0c/p` 8kHz 单声道可用，`QAUDMOD=3` 生效 |
| 语音 | 纯流量卡无法拨号（`ATD` 返回 OK 但网络侧不建立呼叫），换语音/Volte 卡后走同一代码路径 |
| 短信发送 | 该卡被网络拒绝（`+CMS ERROR`），代码路径已验证到 `AT+CMGS` 提示符与 PDU 提交 |
| SIP | 注册（401→200）、OPTIONS、分机互打（100/180/200/ACK/BYE）全部实测通过 |
| 内存 | 13.3MB（空闲） / 22.6MB（含 RTP、Web、SQLite） |

## 目录结构

```
app/
  main.py            入口与组装
  config.py          JSON 配置（点号路径 + 原子写）
  db.py              SQLite：分机/注册/短信/规则/设置/CDR
  g711.py            G.711 μ/A 律
  rtp.py             RTP 收发、RFC2833
  media.py           20ms 媒体桥、抖动缓冲、ModemPort
  notify.py          邮件 / 钉钉 / Webhook 转发
  sip/message.py     SIP 消息解析与构造
  sip/digest.py      摘要认证
  sip/sdp.py         SDP 构造/解析
  sip/leg.py         SIP 腿（UAS/UAC）状态机
  sip/server.py      监听、注册器、B2BUA 呼叫管理
  ec20/at.py         termios AT 通道（读线程 + 命令队列 + URC）
  ec20/modem.py      模块状态机、通话、短信、UAC 开关
  ec20/pdu.py        SMS PDU 编解码
  ec20/audio.py      arecord/aplay 音频通路
  ec20/sim.py        无硬件模拟器
  web/server.py      JSON API + 静态页面
  web/static/*       面板（原生 JS）
scripts/             bind / prepare / diag
packaging/           systemd 单元、udev 规则
tests/               单元 + 集成测试
```

## 已知边界

- SIP 默认只监听 TCP；UDP 需显式开启（`sip.enable_udp`）。不支持 TLS（省内存的取舍）。
- 每路通话消耗 2 个 RTP 端口，端口池见 `sip.rtp_ports`。
- 单腿只协商 PCMU/PCMA（8kHz），不支持 G.722/Opus 转码。
- 群振最多 8 个分机，第一个 200 OK 胜出。
- 本服务不碰路由/NAT 设置；EC20 的数据拨号（`quectel-CM`）与本项目互不影响，可同时使用。

## License

MIT，见 [LICENSE](LICENSE)。
