# RelayMind：OpenClaw 个人推荐操作

[English](README.md#native-recommendations-and-explicit-actions) · [目录配置](../../docs/asset-selection.zh-CN.md)

需要固定的 OpenClaw2026.10.7 和包内声明的 Node 版本。推理提供方仍使用
`relaymind-client-tools`；OpenClaw 继续负责工具、权限和审批。

当前实现包含原生控制台页面、明确选择、反馈隐私及准确的技能版本选择。
**内置资格注册表为空时，生产操作 RPC 不会注册。** 共享令牌、普通聊天、
伪造用户字段或 `qualified:true` 配置都不能绕过这个限制。确定性浏览器/
网关测试只证明机制；实际个人身份、撤销、指令使用及平台验收分别记录。

## 安装和配置

```bash
cd integrations/openclaw
npm ci
npm run pack:release -- /absolute/private/release-directory
openclaw plugins install /absolute/private/release-directory/relaymind-openclaw-provider-0.1.0.tgz
```

保留发行目录中的 SHA256SUMS 和 release.json；安装包包含中英文说明。
先按照[英文配置示例](README.md#native-recommendations-and-explicit-actions)
设置宿主的个人角色边界及插件 `recommendations`。必须使用实际已认证账号/
规范个人档案、配对设备、准确的 controlUiOrigin 和宿主解析的 agent 工作区。
`userId/workspaceId` 必须与该 RelayMind 实例及受限操作注册文件一致；一个
Gateway 实例仍只有一个活动范围。

个人角色设置 `sessions.others:"none"`、实际 agent 集合，以及所需的
operator.read/write/approvals/questions。所有可分配角色都必须保留此限制；
不得加入 operator.admin。共享 owner 或写权限不等于个人会话所有权。

应用令牌和独立操作令牌仅放在宿主进程的受保护环境中，不得放入配置值、
模型消息、工具参数、URL 或日志。操作令牌由本地操作员生成至少32随机字节；
Gateway 只存 SHA-256 及既有注册元数据。`qualified_contract_revision` 必须为
`openclaw-native-2026.10.7.v1`；host_identity/session_mapping 仍是操作员引用，
不是自证资格。仅在内置资格条目确实支持该实现时启用。设置
`[user_actions].registrations_path`，使用 Unix 所有者专用权限或原生 Windows
用户限制 ACL。不存在 HTTP 自注册。Gateway 重启后更新应用令牌环境。

使用经过身份认证且已配对的个人账号登录宿主控制台。原生插件资源要求
HTTPS 或浏览器信任的回环地址；局域网普通 HTTP 的配对成功不代表资源加载
通过。在 **Settings → Labs → Custom plugin UI** 启用自定义 UI，或由操作员设置
`gateway.controlUi.experimental.customPlugins=true`。打开个人会话，再在侧栏
点击 **RelayMind 推荐**；会话操作菜单也能打开同一面板。页面通过正式的原生
`host.request` 连接发送请求，不要求开发者控制台、复制设备密钥或假冒身份。

## 推荐、选择与隐私

1. 输入中文、英文或混合语言任务，点击**推荐**。检查候选、来源版本与真实
   可用性；未知不会被改写成可用。
2. 点击**选择 N**。低置信度 choice_required 留给用户选择；confident 也不会
   自动执行。**均不选择**及**取消推荐**均为明确操作。回执只记录偏好。
3. 点击**有帮助**、**没有帮助**或**纠正为 N**才会记录反馈。选择、沉默、模型
   建议及任务成功都不会自动训练适配。
4. 使用**查看反馈**、逐条**删除反馈**或**清除反馈**。隐私操作保留必要的无内容
   审计回执，并擦除相应的敏感反馈事件。版本冲突需重新读取，不能编造版本。
5. 请求中断时，先前操作可能已经提交。**重试上次请求**保留原始请求及幂等键；
   不会悄悄重试或声称回滚。断开连接会清除面板中的私有内容。

## 准确选择本机技能

在宿主 **Plugins → Skills** 中导入或创建有权使用的技能。操作员在 `skills`
中明确关联 assetId、contentHash、sourceVersion、skillId、revision（字段及
JSON 示例见英文说明）。使用实际推荐记录和本机库 UUID/revision；sourceVersion
必须完全一致。原生 revision 标识完整包，不是 SKILL.md 内容哈希。保留原始
UTF-8 字节及换行；不同内容会拒绝，不得改写预期哈希来掩盖差异。

先明确选择推荐技能，再点击**选择本机技能**，从实际原生库列表选取对应项，
最后点击**供下一轮使用**。适配器核对已提交选择、范围、到期时间、来源版本、
实际个人/会话/工作区、准确的原生 activate/read 回执及指令字节。成功仅表示
“为下一轮选择”，不等于已经加载指令或任务成功。在同一原生会话发送下一条
普通任务；实际指令使用与任务结果需要另外的观测证据。

版本改变、技能删除、会话重置或权限撤销会阻止后续使用。激活后的读取失败
可能已改变下一轮选择，错误会如实保留此事实。已经进入对话的指令无法追溯
清除；需要隔离旧指令时请使用新会话并重新推荐、选择。

MCP 可以作为发现候选并记录偏好，但此适配器尚未启用原生 MCP 执行。安装、
server/original/exposed/schema 准确绑定、权限、审批及执行继续归宿主管理。
页面不会将 toolSchema 视为执行权限，也不会自动启动服务器。提供方行为及
`managedSkillSelection:false` 保持不变。
