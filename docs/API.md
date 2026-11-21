# Web API

所有接口返回 JSON。除 `POST /api/login` 外均需会话 Cookie `sid`（未登录返回 401）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/login` | `{username,password}` → `{ok:true}` + Set-Cookie |
| POST | `/api/logout` | 清除会话 |
| GET | `/api/status` | 主机/模块/SIP/短信/转发 汇总 |
| GET | `/api/system/info` | python、内核、架构、RSS、安装路径 |
| GET | `/api/logs?tail=200` | 内存环形日志 |
| GET | `/api/extensions` | 分机列表（含在线状态、Contact） |
| POST | `/api/extensions` | `{number,display_name,password,enabled}` |
| PUT | `/api/extensions/{id}` | `{display_name,password,enabled,forward_to}`（password 空=不变） |
| DELETE | `/api/extensions/{id}` | 删除并清理注册 |
| POST | `/api/extensions/{id}/kick` | 踢下线（清注册，话机需重新注册） |
| POST | `/api/extensions/{id}/reset_password` | 重置为随机 8 位 |
| GET | `/api/sms?limit=&offset=&q=&unread=` | `{items,total}` |
| POST | `/api/sms/send` | `{to,text}`，长短信自动分片 |
| POST | `/api/sms/read_all` | 全部标记已读 |
| POST | `/api/sms/{id}/read` | 单条已读 |
| POST | `/api/sms/{id}/forward` | 手动按规则转发 |
| DELETE | `/api/sms/{id}` | 删除 |
| GET | `/api/rules` | 转发规则 |
| POST | `/api/rules` | `{name,match_type,pattern,channels,template,enabled}` |
| PUT | `/api/rules/{id}` | 同上 |
| DELETE | `/api/rules/{id}` | 删除 |
| POST | `/api/rules/{id}/test` | 用样例短信跑一遍规则 |
| GET | `/api/settings` | 密钥字段以 `pass_set/secret_set` 表示 |
| PUT | `/api/settings` | 局部合并；密码/密钥留空表示不修改 |
| POST | `/api/settings/test_email` | `{to}` 可省略（默认发件人） |
| POST | `/api/settings/test_dingtalk` | 发送测试消息 |
| POST | `/api/settings/test_webhook` | 发送测试事件 |
| GET | `/api/cdr?limit=50` | 通话记录 |
| POST | `/api/dial` | `{number}` → `{ok,call_id}` |
| POST | `/api/calls/{id}/answer` | 接听 |
| POST | `/api/calls/{id}/hangup` | 挂断 |
| POST | `/api/modem/init` | 重跑 AT 初始化 |
| POST | `/api/modem/reset` | 重开 AT 通道 |

## `GET /api/status` 结构

```json
{
  "uptime": 1234,
  "version": "1.0.0",
  "host": {"mem_total_kb": 919708, "mem_avail_kb": 597776, "load": "0.42 0.38 0.31",
           "temp_c": 48.0, "disk_free_mb": 4396, "uptime": "23m37s", "rss_kb": 22628},
  "modem": {"present": true, "model": "EC20F", "firmware": "EC20CEHCLGR08A04M1G",
            "imei": "8626...", "iccid": "8948...", "sim_state": "READY",
            "signal_dbm": -85, "signal_bars": 4, "csq": 14,
            "reg_status": "已注册(本地)", "operator": "CHN-CT", "ims": false,
            "voice_ready": true, "calls": [], "last_error": ""},
  "calls": [],
  "sip": {"listening": "192.168.1.188:5060", "registrations": 1, "extensions": 2, "active_calls": 0},
  "sms": {"total": 12, "unread": 3, "today": 1},
  "forward": {"enabled": true, "channels": ["email", "dingtalk"]}
}
```

## 转发规则字段

| 字段 | 取值 |
| --- | --- |
| `match_type` | `all` / `sender` / `keyword` |
| `pattern` | `sender` 时是号码片段（逗号/空格分隔）；`keyword` 时是关键词（中英文逗号分隔） |
| `channels` | `email,dingtalk,webhook` 的组合 |
| `template` | 可含 `{peer}` `{text}` `{time}`；留空用默认中文模板 |

## 错误约定

失败统一 `{"ok": false, "error": "中文说明"}`，HTTP 码：400 参数、401 未登录、404 不存在、
409 冲突（分机号重复）、502 设备/网络失败（如短信发送被运营商拒绝）。
