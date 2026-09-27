# PAA · 会成长的个人助手

> **正式实现规范：[PAA Spec v0.2](docs/spec.md)。**
> Mac 端只维护需求与设计；在目标 Ubuntu 直接开发。核心为 Hermes 网关与 Pi 协作。
> 下文及当前代码描述旧参考原型，不代表 v0.2 已实现，也不是正式实现的强制起点。

面向 TU/e 学生的 Ubuntu 24.04 常驻助手。手机入口为 Telegram；Canvas 和 Outlook
使用专用浏览器正常登录会话。当前版本为 **可部署开发版 v0.1，尚未经真实学校账号联调**。

## 已实现

- Playwright 持久浏览器 profile、正常会话周期读取、登录失效检测。
- Canvas 活跃课程、作业、公告、页面、PDF/text附件的会话读取适配器。
- Markdown原文快照和来源链接、资料版本检测、SQLite状态/检索、个人资料索引。
- 明确 Canvas deadline 自动形成日程；正文/PDF可选模型提取、有原文证据的候选日期待确认。
- Telegram私人用户限制、资料搜索、状态、简报、日期确认、草稿修改及版本确认。
- Google OAuth桌面登录、专用日历事件确定ID、重试去重。
- Outlook读取/发送可配置UI适配器，未校准时明确不工作，不假报完成。
- €20月度模型预留账本、关闭模型时仍保留同步与已知提醒。
- 项目内课程规则skill与Ubuntu systemd用户服务模板。

## 当前限制

- 没有安装 Hermes/OpenClaw/Pi，也没有自动部署远程电脑。核心循环直接运行，未来可由 agent
  调用 CLI；Telegram 当前是命令式交互，没有任意自然语言工具执行。
- Outlook默认选择器为空；邮箱扫描仅可见行，历史/虚拟滚动/附件尚未实现，需目标页面校准。
- Outlook发送仅实现新邮件、无附件；实际发送默认关闭，尚未验证租户页面。
- 外部课表/ICS、Canvas外部LTI、扫描PDF OCR和其他附件格式尚未覆盖；课程全覆盖需联调核对。
- 来源删除、改期冲突会暂停相应事件更新并标记stale，暂不自动删除旧日历事件。
- Telegram内的规则提案审批、手机重新认证和自动生成新skill尚未实现。
- 模型账本是保守成本预留；真正账单硬上限还需供应商端限制。

这些缺口是部署状态的一部分，不把部分采集解释为“已掌握全部课程”。

## 开始使用

技术选择、完整需求和分阶段计划见 [技术路线文档](docs/technical-roadmap.md)。
部署操作详见 [Ubuntu部署与配置](docs/ubuntu.md)。快速查看本地CLI（无需安装依赖）：

```sh
python3 -m paa.cli --help
python3 -m unittest discover -s tests -v
```

生产配置从 `config.example.toml` 复制到被Git忽略的 `config.toml`。
所有私人资料和浏览器授权在 `.runtime/`，不进入Git。
当前只有示例配置，没有任何真实账号或密钥。

## 设计

```text
Canvas / Outlook 已登录浏览器    指定的个人Markdown目录
                ↓                     ↓
        原文快照 + 版本索引 + SQLite状态
                ↓
结构化deadline / 受预算约束的模型提取（读取个人资料、skill）
                ↓
       已确认事件 / 待确认提案 / 邮件草稿
                ↓
Google专用日历       Telegram简报、检索、编辑、最终确认
```

素材只充当数据，不能授权操作。邮件发送由程序验证具体版本；日期需对应原文引用。
一般纠正不会自动成为全局规则，经过确认的长期经验保存在
[course-assistant skill](skills/course-assistant/SKILL.md)。

## 实现参考

- [Playwright持久上下文](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)
- [TU/e Canvas API文档](https://canvas.tue.nl/doc/api/)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Google桌面OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)

浏览器会话不等于永久认证，学校要求MFA时仍需本人处理。
