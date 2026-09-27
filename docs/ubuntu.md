# Ubuntu 24.04 部署（旧原型）

> 本文仅适用于现有参考原型。正式 Hermes + Pi 实现须在目标 Ubuntu 按
> [Spec v0.2](spec.md) 开发，并在当地验证后更新部署手册。不要将本文视作新架构安装指南。

本地开发完成不等于已接入账号。以下命令由你在目标 Ubuntu 上执行。
默认部署目录为 `~/PAA`；换目录时同步修改 service 的路径。

## 1. 安装

```sh
sudo apt update
sudo apt install python3-venv python3-pip
cd ~/PAA
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m playwright install --with-deps chromium
cp config.example.toml config.toml
```

Chrome/Edge 已安装时可在 browser.channel 选择 chrome/msedge。
浏览器配置只属于 PAA；不要指定日常浏览器 profile，不开放远程调试端口。

## 2. 手动登录

```sh
.venv/bin/paa login all
```

在弹出的浏览器完成 Canvas 和 Outlook 登录/MFA，回终端按 Enter。
令牌、Cookie、数据库和原始资料都保存在被 Git 忽略的 `.runtime/`。
这里使用正常会话，不生成个人 token，不自动输入学校密码或 MFA。
周期读取属于保活尝试，不能延长学校强制设置的认证上限。

Canvas 通过页面的同源 fetch 读取课程 JSON，继承当前会话，不提供 Bearer token。
若学校连会话访问这些数据也禁用，会明确失败；目前没有实现 Canvas 全 DOM 替代适配器。

## 3. Outlook UI 校准

Outlook 租户/语言/版本变化很大，当前配置不猜测选择器。没有选择器时会打开正常网页，
但将邮箱标记为未配置，而不是已同步。需要在 Ubuntu 的专用浏览器开发者工具中确定：

- ready_selector：登录后邮件列表的唯一标记。
- rows_selector：邮件列表中的消息行，不含分组标题。
- row_id_attribute：每封消息稳定且唯一的属性。
- reading_pane_selector：当前正文区域，必须唯一。
- reading_id_attribute：正文区域上与选中行 ID 一致的属性。没有这个属性时需要针对实际
  页面改写适配器，不能取消“当前正文确实属于当前邮件”的检查。

现有读取适配器仅覆盖当前渲染的最多 max_messages 行，不能声称已遍历虚拟滚动、
其他文件夹或全部历史邮件。第一次实际页面校准后应增加滚动、分页和历史同步。
打开邮件可能使其变为已读，这是浏览器阅读的实际副作用。

## 4. Telegram

通过官方 BotFather 创建自己的 bot；将 token 只保存在 Ubuntu 的私有环境文件。
在 config.toml 中填你的数字 user_id、私聊 chat_id，并将 enabled 改成 true。
先对 bot 发 /start。只接受同时匹配 user_id 和 chat_id 的私人消息。

```sh
mkdir -p ~/.config/paa
chmod 700 ~/.config/paa
nano ~/.config/paa/secrets.env
chmod 600 ~/.config/paa/secrets.env
```

文件内容（使用实际值，不要提交 Git）：

```text
PAA_TELEGRAM_TOKEN=你的token
PAA_MODEL_KEY=你的模型密钥
```

命令：/brief、/status、/find 关键词、/sync、/review、/approve 事件ID。
用 /remember 个人事实 保存明确的分组、身份等信息；此命令不修改通用skill。
回复草稿：/draft 文档ID 收件邮箱 → /edit 草稿ID 完整正文 → /show 草稿ID →
/confirm 草稿ID 完整版本哈希。版本不匹配或已确认的指令不能重复生效。

## 5. 个人资料与模型

在 `.runtime/personal/profile.md` 中填写本人姓名、课程和实验分组、偏好等明确事实。
Markdown/text 文件会自动索引；删除文件后会退出检索。只读取配置指定的目录。

model 默认关闭，关闭时仍能同步、检索和识别 Canvas 结构化 due_at；正文/PDF进入待分析队列。
启用时配置兼容 chat-completions 的 HTTPS endpoint、模型名及 input/output 欧元单价。
模型没有浏览器/终端/写文件工具，输出仅能成为有原文证据的候选事件或回复草稿。
PDF提取后保留页码。图片扫描 PDF 不自动 OCR；会报告覆盖缺口。

每次调用前按保守输入字节数和输出 token 上限预留预算；失败调用也不退回预留额。
该账本以你填的价格为前提，不控制供应商隐藏推理、额外工具收费或价格变化。
必须在供应商端再设置硬支出上限，才能保证实际账单不超过 €20。
模型接口必须支持 max_tokens 和 JSON response_format，超大文件暂不自动分块。
初次全课程材料积累可能超预算，超限会排队，不停止普通同步。

## 6. Google Calendar（可稍后启用）

在 Google Cloud 创建 OAuth Desktop App，启用 Calendar API，下载客户端 JSON 至
`.runtime/google-client.json`。在 Google Calendar 手动创建专用日历，复制该日历 ID
到 calendar.calendar_id。禁止配置 primary。

```sh
.venv/bin/paa google-login
```

在同一 Ubuntu 桌面浏览器授权，再设置 calendar.enabled=true。
若 OAuth 应用保持 Testing，刷新令牌可能短期过期；发布状态和账号类型需部署时检查。
事件使用确定 ID，失败后重试不会生成第二条。只更新专用日历内 PAA 标记的事件。
正文/PDF日期暂需 /approve；明确 Canvas due_at 可自动写入。
过时/消失的来源标为 stale，原日历事件保留供人工核对，目前不自动取消或删除。

## 7. 邮件发送（默认关闭）

outlook_send 为可配置适配器，必须在目标页面完成校准后开启。新邮件窗口中填收件人、主题、
正文，再读回实际内容与确认版本逐项比对。resolved_to_selector 必须覆盖全部收件人
（包括抄送/密送）；每个元素的 recipient_attribute 必须给出真实邮箱地址而不是显示名。
任何不匹配都不点击发送。发送前持久记录 sending；超时或进程重启标记 unknown，不能重试。
sent_selector 必须是本次发送后的专属成功状态，不能使用一直存在的“已发送邮件”文件夹标签。
当前不支持附件发送，也不支持 Outlook 原生 Reply 的 conversation threading；它是新邮件
发送适配器。未校准前使用草稿预览，手动在 Outlook 回复。

## 8. 常驻运行

先在桌面终端运行（环境变量需在当前 shell 导出）：

```sh
.venv/bin/paa run
```

完成配置后安装用户 service：

```sh
mkdir -p ~/.config/systemd/user
cp deploy/paa.service ~/.config/systemd/user/paa.service
systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS
systemctl --user daemon-reload
systemctl --user enable --now paa.service
journalctl --user -u paa.service -f
```

服务依赖 Ubuntu 图形会话，锁屏与注销不同；注销会停止服务，重启后需登录桌面。
本版本不承诺未登录桌面时启动有头浏览器。关闭自动休眠应通过你自己的 Ubuntu 电源设置完成。
程序使用 flock 防止登录助手和 daemon 同时占用同一浏览器 profile。
重新认证时：

```sh
systemctl --user stop paa.service
.venv/bin/paa login all
systemctl --user start paa.service
```

当前尚无手机远程浏览器认证界面，Telegram只通知，需在Ubuntu桌面完成认证。
服务恢复后自动补查，已知事件继续保留；电脑离线时无法自行发告警。

## 9. 状态和维护

```sh
.venv/bin/paa status
.venv/bin/paa search deadline
python3 -m unittest discover -s tests -v
```

每日简报20:00 Europe/Amsterdam；当天过点启动会补发当日简报。
Telegram网络结果不明的发送记为 unknown，不自动重复推送（可能漏一条，/brief 可重新查询）。
数据库和sources目录需要备份；备份属于私人数据，不进入代码仓库。
