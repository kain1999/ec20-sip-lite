# 硬件与内核适配（EC20-CE-HCLG / hi3798mv300）

这份文档记录在这台跑了 `4.4.35_ecoo_83082668` 的 hi3798mv300 盒子上，让 EC20 从"插上看不见"到
"SIP 可拨、声卡可用"的全部踩坑点。换别的盒子/固件时按同样顺序排查即可。

## 1. 模块插上后没有任何 ttyUSB

现象：

```sh
$ lsusb
Bus 001 Device 002: ID 2c7c:0125 Quectel Wireless Solutions Co., Ltd. EC25 LTE modem

$ ls /dev/ttyUSB*
ls: cannot access '/dev/ttyUSB*': No such file or directory

$ lsusb -t
    |__ Port 2: Dev 3, If 0, Class=Vendor Specific Class, Driver=, 480M
```

原因：该厂商内核把 `usbserial` / `qcserial` / `usbserial_generic` 都编译进内核，
但**没有 `option.ko`**（`/sys/bus/usb-serial/drivers/option1` 不存在），而且自带表里没有
`2c7c:0125` 这条组合，于是 5 个 vendor 接口全部无人认领。

解决（本项目 `scripts/ec20-bind.sh` 做的就是这件事）：

```sh
echo -n "2c7c 0125" > /sys/bus/usb-serial/drivers/generic/new_id
ls /dev/ttyUSB*        # -> ttyUSB0 .. ttyUSB5
```

注意两点：

- `qcserial/new_id` 写入返回 0 但**不会**绑上（它的 probe 有接口号白名单）。
- 绑定顺序决定了 ttyUSB 编号：接口 2 = AT，接口 3 = modem/PPP。
  如果中途因为脚本误写产生了普通文件 `/dev/ttyUSB2`，编号会整体后移一位，先 `rm -f /dev/ttyUSB2`。

持久化：`packaging/99-ec20.rules` 在 USB `add` 事件里调用 `ec20-bind.sh`，
另有 `ec20-bind.service` 在开机时兜底。设备名用 `ENV{ID_USB_INTERFACE_NUM}` 匹配 ——
这个内核上 `ATTRS{bInterfaceNumber}` 在 `SUBSYSTEM=="tty"` 事件里匹配不到，必须走 ENV：

```
SUBSYSTEM=="tty", ENV{ID_VENDOR_ID}=="2c7c", ENV{ID_USB_INTERFACE_NUM}=="02", SYMLINK+="ec20-at"
```

## 2. 没有声卡（语音必然单通/无声）

```
$ cat /proc/asound/cards
--- no soundcards ---
```

原因：EC20 的 USB 音频类（UAC）默认关闭。

```
AT+QCFG="usbcfg"      ->  +QCFG: "usbcfg",0x2C7C,0x0125,1,1,1,1,1,0,0
                                                                  └── uac = 0
```

开启（会写 NVM，之后每次枚举都带音频接口）：

```
AT+QCFG="usbcfg",0x2C7C,0x0125,1,1,1,1,1,0,1
AT+CFUN=1,1            # 重启模块，USB 重新枚举
```

20 秒后：

```
$ cat /proc/asound/cards
 2 [EC20CEHCLG     ]: USB-Audio - EC20-CE-HCLG
                      Quectel EC20-CE-HCLG at usb-f9890000.ehci-2, high speed
$ ls /dev/snd
controlC2  pcmC2D0c  pcmC2D0p  timer
```

`lsusb -t` 里会多出 `If 5/6/7 Class=Audio Driver=snd-usb-audio`。

## 3. 通话时采集到全静音

按顺序确认：

1. `AT+QAUDMOD=3`（raw PCM；很多固件默认 0，会出杂音或无声）。
2. 音频必须在**通话已接通**（`AT+CLCC` 里 `stat=0`）之后开：`AT+QPCMV=1,2`。
   在 `stat=2`（拨号中）时开 UAC，采到的就是纯静音。
3. 挂断前 `AT+QPCMV=0`，否则下次通话可能拿不到音频。
4. ALSA 设备用 `plughw:<card>,0`（`plughw` 会按需重采样，`hw:` 在只支持 48k 的固件上会失败）。

验证采集链路：

```sh
timeout 4 arecord -D plughw:2,0 -f S16_LE -r 8000 -c 1 -d 2 -q /tmp/t.wav
python3 -c "import wave,audioop;w=wave.open('/tmp/t.wav');print(audioop.rms(w.readframes(w.getnframes()),2))"
```

RMS 恒为 0 且通话确实已接通 → 检查 SIM 是否支持语音（纯流量卡不行），或看 `AT+CIREG?` 的 IMS 注册状态。

## 4. IMS / VoLTE

```
AT+QCFG="ims"        ->  +QCFG: "ims",0,0      # 0,0 = 关闭
AT+QCFG="ims",1      # 开启
AT+CIREG?            # 1 = 已注册 IMS；部分固件不支持该命令（返回 ERROR）
```

中国电信卡建议 `AT+QCFG="ims",1` 后 `AT+CFUN=1,1`。注意本机实测：`AT+CIREG?` 返回 ERROR，
而 `AT+CLCC` 里长期存在一条 `1,1,0,...` 的"幽灵"通话记录（IMS 未注册时的内部状态），
不影响本项目（我们按号码/状态匹配自己的通话）。

## 5. 短信

- 存储：`AT+CPMS="ME","ME","ME"`（255 条）优于默认 `SM`（本机只有 3 条）。
- 中文：`AT+CSCS="UCS2"`，本项目用 **PDU 模式**（`AT+CMGF=0`）收发，编码在 `app/ec20/pdu.py`。
- 新短信通知：`AT+CNMI=2,1,0,0,0` → URC `+CMTI: "ME",<idx>` → 我们用 `AT+CMGR=<idx>` 读取后 `AT+CMGD` 删除。
  这种"索引 + 主动读取"的方式比直接解析 `+CMT` 的多行 PDU 稳，不会和命令响应抢行。
- 长短信：按 UDH 的 `ref/total/seq` 在内存里拼接，30 秒未集齐则丢弃。

## 6. AT 通道实现要点

`app/ec20/at.py` 用 `os.open` + `termios` + `select` 起一个读线程，行通过 `call_soon_threadsafe`
交回 asyncio。踩过的两个坑：

1. **URC 与命令响应同形**：`AT+CPIN?`、`AT+CSQ`、`AT+CLCC` 的响应行同时符合 URC 前缀，
   必须先按命令名推算出期望前缀（`+CPIN`/`+CSQ`/`+CLCC`），在命令在飞时优先作为响应处理，
   否则 `sim_state`/`csq` 永远是空。
2. **回显**：`ATE0` 之前模块会回显命令，需按"最近写入的命令"过滤，否则 `OK` 会被提前匹配。

## 7. 资源占用参考

```
$ systemctl show -p MemoryCurrent ec20-sip
MemoryCurrent=13852672          # 13.2 MB，空闲
```

通话中（1 路 SIP + USB 音频 + RTP 桥）约 22–26MB；`arecord`/`aplay` 各 2MB 左右。
两路并发仍在 30MB 以内，对 500MB 可用内存的盒子来说是安全的。
