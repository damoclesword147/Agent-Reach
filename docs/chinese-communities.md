# 中文社区渠道：百度贴吧与酷安

本 fork 为 Agent Reach 增加两个可选只读渠道，复用上游的渠道注册、依赖隔离、参数校验、统一 JSON、错误处理和验收记录。它们不会随默认 setup 自动开启，不需要为公开检索启动浏览器。

**当前状态：贴吧已通过列帖样本，其他动作仍需验收；酷安后端尚未就绪。** 离线测试能验证契约和数据处理，不能证明目标机器的网络、平台接口或某个帖子当时可读。不能把安装成功、doctor 的依赖就绪或少量成功样本称为全平台覆盖。

## 安装和版本

基础 Agent Reach 的 Python 下限保留为 3.10；贴吧可选工具 aiotieba 4.8.4 要求 Python >=3.11,<3.16，酷安 CLI 要求 Python 3.10+。请使用相容的 Python 安装本 fork 当前分支；上游安装说明仍指向原仓库，不包含此 fork 的渠道。

在本 fork 版本下执行：

```bash
agent-reach add tieba
agent-reach add coolapk
agent-reach channels
agent-reach doctor --text
```

工具在 Reach 的私有目录中管理，不需要把其命令放到全局 PATH。只添加需要的渠道；可用 `agent-reach remove tieba` 或 `agent-reach remove coolapk` 移除。

## 百度贴吧

```bash
# 全吧搜索只覆盖主题
agent-reach tieba search "5070Ti" --limit 5 --page 1
# 吧内搜索必须用准确吧名，不额外加“吧”字
agent-reach tieba search "5070Ti" --forum "显卡" --limit 5 --page 1
# 按回复时间、创建时间或热度浏览主题
agent-reach tieba threads "显卡" --sort reply --limit 10 --page 1
# 读取一页楼层；用实际帖子ID替换占位符
agent-reach tieba read "https://tieba.baidu.com/p/<帖子ID>" --page 2 --limit 20 --comment_limit 5
```

| 动作 | 参数和边界 |
|---|---|
| search | query；可选 forum；limit 1-20，默认 10；page 1-1000，默认 1 |
| threads | forum；sort 为 reply/create/hot，默认 reply；limit 1-100，默认 10；page 1-1000 |
| read | 帖子ID或 HTTPS 帖子链接；limit 2-30，默认 20；page 1-1000；comment_limit 0-5，默认 0 |

read 返回主题信息和请求页的楼层。`--comment_limit` 只控制每楼至多取多少条高赞楼中楼样本；不保证按时间完整分页，也不代表所有回复。结果中保留楼层、pid、thread_id、返回/报告的评论数量和不完整提示；还应根据实际输出核对页码与缺失内容。

## 酷安（实验性）

当前酷安 CLI 在发出请求前需要 App 认证数据。干净安装不会准备该数据；缺少时动作返回 `NEED_SETUP`，联网检查后的 `doctor` 会显示 `needs_setup`。以下为适配器的接口示例，尚未取得真实酷安内容。需要查看社区内容时可使用[酷安官方 App](https://www.coolapk.com/)。

```bash
agent-reach coolapk search "手机" --limit 5 --page 1
agent-reach coolapk hot --limit 5 --page 1
agent-reach coolapk secondhand --brand 1005 --limit 5 --page 1
agent-reach coolapk read "https://www.coolapk.com/feed/<动态ID>"
```

search/hot/secondhand 的 limit 为 1-20，默认 10；page 为 1-100，默认 1。secondhand 可省略 brand，1005 为上游示例中的苹果品牌ID。read 接受正整数动态ID或 HTTPS 动态链接，只读取动态详情，不自动获取评论。

酷安详情接口可能返回验证码，搜索或列表仍可用也不意味着详情可用。列表正文可能被截断或缺少详情专属字段，不能称为完整文章；图片 URL 仅代表取得链接，不代表已理解图片。新渠道不提供点赞、发帖、回复、关注、私信或自动登录。

## 验收与失败处理

```bash
agent-reach check tieba --text
agent-reach check coolapk --text
```

check 会联网执行渠道已声明的测试动作；未配置测试样本的 read 动作仍须用真实链接单独核验。doctor 只报告离线状态和已有验收记录，不会通过一次探测保证所有动作可用。结果、错误码与本机状态以当前输出为准。

- 检查 JSON 的 ok、channel、action、backend、items、meta 和 error；不要把错误消息或验证页面当正文。
- NEED_SETUP 表示工具依赖或后端前置条件未满足；TIMEOUT 表示这次请求超时，不能由此推断平台必须登录。
- PLATFORM_BLOCKED、RATE_LIMITED 或 NEED_LOGIN 应结束本次自动尝试，保留已有结果，并按错误说明处理。不要自动改身份、换后端或反复请求。
- 引用内容时保留来源链接，区分搜索摘要、列表正文、指定页楼层与楼中楼样本；不要把有限结果总结成完整评论树。

## 本机联网验收记录（2026-11-24）

使用仓库内隔离的测试 HOME 和私有依赖环境，未读取个人账号或浏览器登录态。以下只描述该次请求的结果，不代表其他机器、网络出口或后续请求：

| 动作 | 本次结果 |
|---|---|
| 贴吧 search 全吧，query=python，limit=1 | 返回真实主题 |
| 贴吧 search 吧内，显卡吧，query=5070Ti | 返回真实主题 |
| 贴吧 threads，显卡吧，limit=1 | 返回真实主题 |
| 贴吧 read，limit=3，comment_limit=2 | 返回主题和楼层 |
| 酷安 hot | NEED_SETUP，未取得平台内容 |

2026-11-20 的记录中全吧搜索和 read 均为 TIMEOUT。原因不是登录：aiotieba 对 HTTPS 接口（全吧搜索、楼层）以 `ssl=False` 发请求，在当前 aiohttp 下这类连接会在建连阶段卡住，HTTP 接口（吧内搜索、主题列表）不受影响。worker 现在为这些请求传入显式 TLS 上下文（与 aiotieba 一致，不校验证书），并把调用方的超时传给 aiotieba，超时时在剩余预算内重试一次。

## 上游参考

- [aiotieba](https://github.com/lumina37/aiotieba)：贴吧客户端及版本要求。
- [coolapk-mcp](https://github.com/Lniosy/coolapk-mcp)：酷安 CLI 与数据模型。
- [酷安详情接口已知限制](https://github.com/Lniosy/coolapk-mcp/issues/5)：验证码和字段完整性边界。
