// 由 scripts/gen-api.mjs 从 openapi.json 生成，不要手改

export interface paths {
    "/api/auth/login": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 用密码登录，成功后下发登录 cookie。连续输错同一 IP 要等一段时间才能再试（429，带 Retry-After）。 */
        post: operations["login"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/logout": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 登出：删掉浏览器里的登录 cookie。 */
        post: operations["logout"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/me": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 已登录时 204，否则 401（前端据此判断要不要显示登录页）。 */
        get: operations["me"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * 事件流：连上先发一次心跳（订阅已建立，前端此时重新拉取不会漏事件），之后有事件就发，空闲满一个间隔发心跳；服务关停
         *     （总线关闭）或跟不上被断开时结束，浏览器自己重连。断线重连的间隔用浏览器的默认值。
         */
        get: operations["events"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agent-defaults": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 新建 agent 时表单预填的值：创建时套用的同一份缺省（AgentSpec 的缺省，模型取服务端配置），前端不另写一份。 */
        get: operations["agent_defaults"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agent-preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * agent 配置（还没保存的也行）最终能用的：自己的资产 + base + 角色 + allow / deny 放行的，与运行时同一套计算，与保存时
         *     同样校验（不合法 422）。
         */
        post: operations["preview_agent"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/roles": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 全部角色：内置在前，自建的按名称。 */
        get: operations["list_roles"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/role-preview": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 角色规则（还没保存的也行）能用的，与保存时同样校验（不合法 422）。 */
        post: operations["preview_role"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/roles/{name}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["create_role"];
        /** 有 agent 引用时 409。 */
        delete: operations["delete_role"];
        options?: never;
        head?: never;
        /** 改角色的描述与可编辑的规则（内置角色也可以，固定的部分不变）；从带它的 agent 的下一次运行起生效。 */
        patch: operations["update_role"];
        trace?: never;
    };
    "/api/agents": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["list_agents"];
        put?: never;
        post: operations["create_agent"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 正在删除的按不存在处理。 */
        get: operations["get_agent"];
        put?: never;
        post?: never;
        /** 删除 agent：它的会话、触发器、指派等各领域的数据一起清理，资源文件里别人还在用的转给平台。 */
        delete: operations["delete_agent"];
        options?: never;
        head?: never;
        patch: operations["update_agent"];
        trace?: never;
    };
    "/api/agents/{agent_id}/assets": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** agent 能用的记忆、skill、secret、MCP server 与各自的来源（不含 secret 的值），加上记忆合并后放进指令的样子。 */
        get: operations["agent_assets"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/default": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** 设为系统默认 agent（取代原来的默认）。 */
        put: operations["set_default_agent"];
        post?: never;
        /** 取消它的系统默认身份；它不是默认时不做任何事。 */
        delete: operations["unset_default_agent"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/archive": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** 归档：不能再对话、委派、指派，它的触发器不触发；它忙或是默认 agent 时 409。 */
        put: operations["archive_agent"];
        post?: never;
        /** 取消归档；没归档时不做任何事。 */
        delete: operations["unarchive_agent"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/memory": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** agent 的私有记忆（正在删除的按不存在处理）。 */
        get: operations["get_private_memory"];
        /** 整体改写 agent 的私有记忆（没有时新建）。只有它自己用；规则不按名字指它。 */
        put: operations["put_private_memory"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/assets": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * 全部资源：内置工具（当前配置下注册的，按注册顺序）在前，存成文件的按种类、名称；每种带自己的详情（工具的读写与管理类、
         *     MCP 的配置与启停）。
         */
        get: operations["list_assets"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/assets/{kind}/{name}/owner": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** 挪到另一个 owner 的目录下；内容与收录它的角色不变。记忆不转移（422）；内置工具归平台，不在这里（kind 不接受 tool）。 */
        put: operations["transfer_asset"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/assets/memory/{name}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 平台的一块记忆（全局或共享）。 */
        get: operations["get_memory"];
        /** 新建（名字要是 slug）或整体改写平台的一块记忆。谁能用由角色收录它（`use:memory:<名>`）。 */
        put: operations["put_memory"];
        post?: never;
        delete: operations["delete_memory"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/assets/skill/{name}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_skill"];
        /** 新建的归 owner；已有的整体改写正文与描述（附件不变），owner 不变。 */
        put: operations["put_skill"];
        post?: never;
        delete: operations["delete_skill"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/assets/skill/{name}/files": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** skill 目录里 path（相对 skill 目录，空为根）这一层，同工作目录浏览：目录在前，各自按名称排序。 */
        get: operations["list_skill_files"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/assets/skill/{name}/file": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** skill 目录里一个文件的内容（同工作目录浏览：图片 inline，其余作为附件）。 */
        get: operations["read_skill_file"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/assets/mcp/{name}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_mcp"];
        /** 新增（启用的）server；同名的已存在 409（名字全局唯一，配置不能原地改写：删了再建）。 */
        put: operations["add_mcp"];
        post?: never;
        delete: operations["delete_mcp"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/assets/mcp/{name}/enabled": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put: operations["set_mcp_enabled"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/assets/mcp/{name}/tools": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 连接一次，列出它的工具（测试连接）。 */
        post: operations["probe_mcp"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/assets/secret/{name}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 不含值。 */
        get: operations["get_secret"];
        /** 新建的归 owner（缺省为平台）；已有的改值与描述，owner 不变。 */
        put: operations["set_secret"];
        post?: never;
        delete: operations["delete_secret"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/providers": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 配置里的 LLM provider 名，即 agent 模型 "<provider 名>:<模型名>" 的前半部分。 */
        get: operations["list_providers"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/providers/{name}/models": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 向该 provider 实时查询的模型名；provider 出错时为 502 并带上原因。 */
        get: operations["list_provider_models"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/features": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 服务端配置决定的界面行为：可选能力。 */
        get: operations["features"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/sessions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 工作状态不是 idle 的会话（在全部符合筛选的会话里，不只是这一页）置顶，其余按最近更新排序。 */
        get: operations["list_sessions"];
        put?: never;
        /** 新建会话并发出第一条消息。 */
        post: operations["start_session"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/sessions/{session_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_session"];
        put?: never;
        post?: never;
        /** 删除会话（它开出的 thread 保留）；会话忙时 409。 */
        delete: operations["delete_session"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/sessions/{session_id}/messages": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 在会话里发一条消息（续写）、回答一次提问，或批准一次调用。返回信进的会话。 */
        post: operations["reply"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/sessions/{session_id}/stop": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 停止这个会话：结束正在进行的运行（已经产生的部分照常保存）、清空信箱，连带停下它发起的异步工作。返回时已生效。 */
        post: operations["stop"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/sessions/{session_id}/compact": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * 手动压缩上下文：排在正在进行的运行之后，在后台做；开始与做完时会话页随 saved 事件刷新。没有可压缩的（都在保留的最近几步里）
         *     时什么都不变；失败记在服务日志里。
         */
        post: operations["compact"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/sessions/{session_id}/retry": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 重试最近一次最终失败的运行（同一次运行接着跑）。会话在跑、或最近一次运行没有失败时 409。 */
        post: operations["retry"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/sessions/{session_id}/read": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 标记会话已读：会话的未读就是指向它的未读通知，由通知领域标为已读（幂等，会话不存在时什么都不做）。 */
        post: operations["mark_session_read"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/files": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 请求体就是文件内容，Content-Type 是它的类型（与 Matrix /media/upload 相同，不用 multipart），边读边写，超限即停。 */
        post: operations["upload"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/files/{file_id}/{name}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 文件内容；每个文件只有一个规范地址（名字对不上 404）。内容按 id 永不改变，可以一直缓存。 */
        get: operations["download"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/workspace": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 列出工作目录里 path（相对工作目录，空为根）这一层：目录在前，各自按名称排序。 */
        get: operations["list_workspace"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/workspace/file": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 工作目录里一个文件的内容。 */
        get: operations["read_workspace_file"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/script-grants": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** skill 脚本的「始终允许」（#531），新的在前。 */
        get: operations["list_script_grants"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/script-grants/{grant_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /** 撤销一条始终允许：之后同一调用重新要批准。 */
        delete: operations["revoke_script_grant"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/jobs/{job_id}/kill": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 结束这个 agent 的一个运行中的后台任务；结局（被结束）照常作为事件回到发起它的会话。已经结束的 409。 */
        post: operations["kill_job"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/issues": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["list_issues"];
        put?: never;
        post: operations["create_issue"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/issues/{issue_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_issue"];
        put?: never;
        post?: never;
        delete: operations["delete_issue"];
        options?: never;
        head?: never;
        patch: operations["update_issue"];
        trace?: never;
    };
    "/api/issues/{issue_id}/comments": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["comment_issue"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/issues/{issue_id}/approve": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 审核通过指派给用户的 issue：设为 done。没有指派给用户时 409。 */
        post: operations["approve_issue"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/issues/{issue_id}/start": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 开工 backlog 里的 issue：指派给 owner 并进入 in_progress。不在 backlog 或没有 owner 时 409。 */
        post: operations["start_issue"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/issues/{issue_id}/reject": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * 打回指派给用户的 issue：理由记成评论，回到 in_progress，指派给把它交给用户的 agent。没有指派给用户、或不是 agent 交来的
         *     时 409。
         */
        post: operations["reject_issue"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/trigger-kinds": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 登记的触发方式。 */
        get: operations["trigger_kinds"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/triggers": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["list_triggers"];
        put?: never;
        post: operations["create_trigger"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/triggers/{trigger_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        delete: operations["delete_trigger"];
        options?: never;
        head?: never;
        patch: operations["update_trigger"];
        trace?: never;
    };
    "/api/triggers/{trigger_id}/run": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * 立即触发一次（不带事件内容）。有前置检查时同样先跑检查，可以用来测试整条链路（检查失败 422 带原因）。不消耗一次性
         *     触发器预定的那一次；已结束的触发器不能再运行。
         */
        post: operations["run_trigger"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/notifications": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 通知，新的在前。 */
        get: operations["list_notifications"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/notifications/unread": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["unread"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/notifications/read": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["mark_notifications_read"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/notifications/issues/{issue_id}/read": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 打开了 issue：请用户审核它的通知标为已读。 */
        post: operations["mark_issue_read"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/push/vapid": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["vapid_key"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/push/subscriptions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 登记这台设备的推送订阅（同一个 endpoint 再登记是更新）；订阅不合格式（不是 https、密钥解不开）422。 */
        post: operations["subscribe"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/push/test": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 发一条测试通知（推到每台设备）。 */
        post: operations["test_push"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/usage": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * [since, until) 之间的用量，按给出的维度分组。「天」按服务端配置的时区划分日历日，与客户端所在时区无关。input_tokens 是
         *     输入总数，已包含 cache_read_tokens 与 cache_write_tokens，展示时是总数与其中两部分的关系，不要相加。没有参与分组的维度
         *     为 null。
         */
        get: operations["usage_summary"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/history": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** agent 的工作历史：没给 query 按日期倒序；给了按关键词检索（规则同工具 history_search）。since / until 含两端。 */
        get: operations["agent_history"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/transcriptions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** 请求体是一段语音（webm、ogg、mp4、wav、mp3……），返回转写出的文字；转写服务出错时 502 并带上原因；没配置语音转写时 404。 */
        post: operations["transcribe"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/pages": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** 全部 agent 的页面：按 agent 列表的顺序，各自按页面名排序。 */
        get: operations["list_pages"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /** @enum {string} */
        Access: "read" | "write";
        /** @description 时间线上记下的一条：谁、什么时候、在哪个会话里，发生了什么。 */
        Activity: components["schemas"]["Happening"] & {
            /** Format: int64 */
            id: number;
            /** Format: int64 */
            issue_id: number;
            /** Format: date-time */
            created_at: string;
            /** @description 做这件事的人：用户、agent，或系统自己的清理 */
            actor: components["schemas"]["Actor"];
            session_id: components["schemas"]["SessionId"] | null;
        };
        /**
         * @description 做一件事的人：用户、系统自己的清理（删除 agent），或一个 agent。库里存成 `user`、`system` 或 agent id（只经
         *     [`Self::as_str`] / [`Self::from_stored`]）；JSON 按种类打标签。
         */
        Actor: {
            /** @enum {string} */
            kind: "user";
        } | {
            /** @enum {string} */
            kind: "system";
        } | {
            agent_id: components["schemas"]["AgentId"];
            /** @enum {string} */
            kind: "agent";
        };
        /**
         * @description 被问的 agent 交回的结局：给发问方模型的文字，它挂给用户的文件（交回时挂到发问方这次调用的结果上，文件就出现在用户
         *     正在看的会话里）。
         */
        AgentAnswer: {
            text: string;
            attachments: components["schemas"]["SharedFile"][];
        };
        /** @description agent 能用的资产，按种类分组，各组按名称。 */
        AgentAssetsOut: {
            memories: components["schemas"]["AssetOut"][];
            /** @description 记忆注入 instructions 的那一段（与运行时同一个渲染函数） */
            memory_preview: string;
            skills: components["schemas"]["AssetOut"][];
            secrets: components["schemas"]["AssetOut"][];
            mcp: components["schemas"]["McpAssetOut"][];
        };
        /** @description agent.toml 里的配置。 */
        AgentConfigOut: {
            description: string;
            model: string;
            thinking: components["schemas"]["Thinking"];
            /** @description 分组名，null 为不分组 */
            group: string | null;
        };
        AgentCreate: {
            name: string;
            description: string;
            /** @description 缺省（null）时使用服务端配置的默认值 */
            model?: string | null;
            /** @description 角色，不含每个 agent 隐含的 base */
            roles?: string[];
            allow?: string[];
            deny?: string[];
            /** @description 思考档位："default" 不指定，按模型与服务端的默认 */
            thinking?: components["schemas"]["Thinking"];
            /** @description 分组名；null 或空串为不分组 */
            group?: string | null;
        };
        /** @description 新建 agent 时表单预填的值。 */
        AgentDefaults: {
            name: string;
            description: string;
            model: string;
            roles: string[];
            allow: string[];
            deny: string[];
            thinking: components["schemas"]["Thinking"];
            /** @description 分组名；null 为不分组 */
            group: string | null;
        };
        /** @description agent 的编号（agents 表的 id，也是它资源目录、工作目录的名字） */
        AgentId: string & {
            readonly __brand: 'AgentId';
        };
        AgentOut: {
            id: components["schemas"]["AgentId"];
            name: string;
            config: components["schemas"]["AgentConfigOut"] | null;
            roles: string[];
            allow: string[];
            deny: string[];
            /** Format: date-time */
            created_at: string;
            /** @description 系统默认 agent：Web 新对话默认选中它，最多一个 */
            is_default: boolean;
            /**
             * Format: date-time
             * @description 归档时间；非空时不能对话、委派、指派，它的触发器不触发
             */
            archived_at: string | null;
        };
        AgentPreviewIn: {
            agent_id?: components["schemas"]["AgentId"] | null;
            roles: string[];
            allow: string[];
            deny: string[];
        };
        /** @description 只改给出的字段（缺省或 null 是不变）：界面只提交用户改过的，编辑期间 agent 自己改了另一个字段不会被旧值盖掉。 */
        AgentUpdate: {
            name?: string | null;
            description?: string | null;
            model?: string | null;
            roles?: string[] | null;
            allow?: string[] | null;
            deny?: string[] | null;
            thinking?: components["schemas"]["Thinking"] | null;
            /** @description 空串为不分组 */
            group?: string | null;
        };
        AnsweredQuestion: {
            question: string;
            selected: string[];
            other: string | null;
        };
        /**
         * @description 停下来等用户批准的工具调用的状态。
         * @enum {string}
         */
        ApprovalState: "pending" | "submitted";
        /** @description 不是属性变化、只有一个值的记法（存为 `{"from": null, "to": 值}`）。 */
        Arrival: {
            /** @default null */
            from: unknown;
            to: components["schemas"]["AgentId"];
        };
        /** @description 一次提问交给模型的结果的存下形状（模型看到的、库里的历史与回答信件、接口里的都是它）：answered 为假时 note 是说明。 */
        AskResult: {
            answered: boolean;
            answers: components["schemas"]["AnsweredQuestion"][];
            note: string | null;
        };
        /** @description ask_user 调用的状态，由后端按唯一规则给出（[`annotate_asks`]），前端只渲染。 */
        AskState: {
            /** @enum {string} */
            status: "asking";
        } | {
            /** @enum {string} */
            status: "pending";
        } | {
            /** @enum {string} */
            status: "submitted";
        } | {
            answers: components["schemas"]["AnsweredQuestion"][];
            /** @enum {string} */
            status: "answered";
        } | {
            note: string;
            /** @enum {string} */
            status: "not_answered";
        };
        AskView: {
            questions: components["schemas"]["Question"][];
            state: components["schemas"]["AskState"];
        };
        AssetOut: {
            name: string;
            owner: components["schemas"]["AgentId"] | null;
            description: string;
            source: components["schemas"]["AssetSourceOut"];
            /** @description 它能改（write） */
            editable: boolean;
        };
        /** @description 为什么能用：它名下的、放行它的角色（含 base）、它自己的 allow 规则（至少一项）。 */
        AssetSourceOut: {
            own: boolean;
            roles: string[];
            allow: boolean;
        };
        /**
         * @description 指派人：用户（只有一个），或一个 agent。库里存成 `user` 或 agent id（12 位十六进制，不会与 `user` 相同；agent 名称也
         *     不能取 `user`，工具里按名称指派时不会混淆），读写库只经 [`Self::as_str`] / [`Self::from_stored`]；JSON（接口与时间线的
         *     change）按种类打标签。未指派是 `Option::None`。
         */
        Assignee: {
            /** @enum {string} */
            kind: "user";
        } | {
            agent_id: components["schemas"]["AgentId"];
            /** @enum {string} */
            kind: "agent";
        };
        /** @description 一个文件的展示形式（界面用）。 */
        Attachment: {
            /** @description 文件存储的地址（files::file_url）：界面直接用它显示与下载 */
            url: string;
            name: string;
            media_type: components["schemas"]["MediaType"];
            /** @description 能内联显示的图片；其余是下载链接 */
            inline: boolean;
        };
        /** @description 看板上的一个 issue：它本身加上谁在做（HTTP 返回的形状）。 */
        BoardIssue: components["schemas"]["Issue"] & components["schemas"]["IssueWork"];
        /**
         * @description 上一次触发开出的会话还没做完时它在干什么（不会是 idle）。
         * @enum {string}
         */
        Busy: "running" | "waiting";
        CallItem: {
            tool_name: string;
            tool_call_id: string;
            /** @description 调用参数原文 */
            text: string;
            /** @description 开出或续写的 thread（子会话 id），由会话接口按调用 id 填（[`annotate_threads`]） */
            threads: components["schemas"]["SessionId"][];
            wait: components["schemas"]["CallWait"] | null;
            /** @description 参数按工具自己的展示规则画成的展示块；None 显示原文（工具没定规则、参数不合它的类型） */
            view: components["schemas"]["ViewBlock"][] | null;
            /**
             * Format: date-time
             * @description 开始执行的时间；调用方没给保存时间的投影为 None
             */
            at: string | null;
        };
        /** @description 回答信件里一次调用的结果。 */
        CallResult: {
            result: components["schemas"]["AskResult"];
            /** @enum {string} */
            tool: "ask_user";
        } | {
            result: components["schemas"]["AgentAnswer"];
            /** @enum {string} */
            tool: "ask_agent";
        } | {
            result: components["schemas"]["Decision"];
            /** @enum {string} */
            tool: "approval";
        };
        /** @description 工具调用要用户操作的那一面：提问（ask_user，参数合法时），或停下来等批准（由会话接口填，[`annotate_approvals`]）。 */
        CallWait: (components["schemas"]["AskView"] & {
            /** @enum {string} */
            kind: "ask";
        }) | {
            state: components["schemas"]["ApprovalState"];
            /** @enum {string} */
            kind: "approval";
        };
        /** @description 资源池里的一项，在规则里写作 `<action>:<kind>:<name>`；按 kind 区分的详情。 */
        CatalogAssetOut: components["schemas"]["CatalogDetail"] & {
            name: string;
            owner: components["schemas"]["AgentId"] | null;
            description: string;
            /** @description 收录它的角色（allow 放行、deny 没拦，含内置的 base、admin） */
            roles: string[];
            /**
             * @description 分发出去了：base 收录它，或某个不是 owner 的 agent 的规则（角色或自己的 allow）放行它；否则只有 owner 能用（归平台的
             *     就谁也用不了）。与删除 agent 时把资产转给平台同一个定义
             */
            shared: boolean;
            /**
             * Format: date-time
             * @description 文件的修改时间；内置工具在代码里定义，为 null
             */
            updated_at: string | null;
            /** @description 文件（skill 是目录）的位置；内置工具为 null */
            path: string | null;
        };
        CatalogDetail: {
            access: components["schemas"]["Access"];
            /** @description 管理类工具：base 角色没有 */
            admin: boolean;
            /** @enum {string} */
            kind: "tool";
        } | {
            /** @enum {string} */
            kind: "skill";
        } | (components["schemas"]["McpConfigOut"] & {
            /** @enum {string} */
            kind: "mcp";
        }) | {
            /** @description false：agent 自己写的命令不能注入，只有 skill_run 的 skill 脚本与 MCP 配置能用（#528） */
            allow_shell: boolean;
            /** @enum {string} */
            kind: "secret";
        } | {
            /** @enum {string} */
            kind: "memory";
        };
        /** @description 属性从什么改成什么（存为 `{"from": ..., "to": ...}`）。 */
        Change_Option_AgentId: {
            from: null | string;
            to: null | string;
        };
        /** @description 属性从什么改成什么（存为 `{"from": ..., "to": ...}`）。 */
        Change_Option_Assignee: {
            from: null | ({
                /** @enum {string} */
                kind: "user";
            } | {
                agent_id: components["schemas"]["AgentId"];
                /** @enum {string} */
                kind: "agent";
            });
            to: null | ({
                /** @enum {string} */
                kind: "user";
            } | {
                agent_id: components["schemas"]["AgentId"];
                /** @enum {string} */
                kind: "agent";
            });
        };
        /** @description 属性从什么改成什么（存为 `{"from": ..., "to": ...}`）。 */
        Change_Priority: {
            /** @enum {string} */
            from: "none" | "urgent" | "high" | "medium" | "low";
            /** @enum {string} */
            to: "none" | "urgent" | "high" | "medium" | "low";
        };
        /** @description 属性从什么改成什么（存为 `{"from": ..., "to": ...}`）。 */
        Change_String: {
            from: string;
            to: string;
        };
        /** @description 属性从什么改成什么（存为 `{"from": ..., "to": ...}`）。 */
        Change_Vec_i64: {
            from: number[];
            to: number[];
        };
        /** @description 服务端数据变了（不分类别）：正在显示的页面重新拉取。 */
        ChangedEvent: Record<string, unknown>;
        /**
         * @description 用户在界面上对等批准的调用的选择。
         * @enum {string}
         */
        Choice: "approve" | "deny" | "always";
        CommentIn: {
            body: string;
        };
        CompactionOut: {
            /**
             * Format: int64
             * @description 这条消息及之前的已由 summary 代替发给模型（原文仍完整展示）
             */
            through_message_id: number;
            summary: string;
            /** Format: date-time */
            created_at: string;
        };
        /** @description 发给模型的上下文有多大、到多大会压缩。 */
        ContextOut: {
            /**
             * Format: int64
             * @description 现在的大小：上一次模型请求的输入 token（含缓存读写）；还没有模型回复、或刚压缩过还没有新回复时为 null；0 表示模型
             *     没有报告用量
             */
            tokens: number | null;
            /**
             * Format: int64
             * @description 上下文超过它就压缩
             */
            threshold_tokens: number;
        };
        /**
         * @description 用户对一次等批准的调用的决定。pin：用户批准的是哪一份（请求批准时工具定下、记在 approval_requests 的指纹），批准时
         *     交回工具比对。迁移来的旧信件没有它或为 null（都已处理，不会再交给运行），读入时当作空指纹：工具给的指纹不会是空的，
         *     比对对不上。
         */
        Decision: {
            choice: components["schemas"]["Choice"];
            pin: string;
        };
        /** @enum {string} */
        DeltaType: "assistant" | "thinking";
        /** @description 一个上游完成（done / cancelled）：它的状态变化，状态的取值同 [`Happening::Status`]。 */
        DependencyChange: {
            /**
             * Format: int64
             * @description 完成的那个上游
             */
            issue: number;
            from: string;
            to: string;
        };
        EnabledIn: {
            enabled: boolean;
        };
        /** @description 目录里的一项：目录或文件，大小与能否内联只有文件有。 */
        EntryOut: {
            name: string;
            /** Format: date-time */
            modified: string;
            /** @enum {string} */
            kind: "dir";
        } | {
            name: string;
            /** Format: date-time */
            modified: string;
            /** Format: int64 */
            size: number;
            /** @description 能内联显示的图片；其余读文件时作为附件返回 */
            inline: boolean;
            /** @enum {string} */
            kind: "file";
        };
        /** @description 失败、参数被拒绝交还模型重试的结果。 */
        ErrorItem: {
            tool_name: string;
            tool_call_id: string;
            text: string;
            /** Format: date-time */
            at: string | null;
        };
        /** @description 错误响应体（各接口的 4xx、5xx 都是它）。 */
        ErrorOut: {
            detail: string;
        };
        /** @description 会话事件：text 是事件内容。 */
        EventItem: {
            text: string;
            source: components["schemas"]["EventSource"];
            /** @description 运行中检查（工作还在进行） */
            check_in: boolean;
            /**
             * Format: date-time
             * @description 交给模型的时间（所在请求的 timestamp）
             */
            at: string;
        };
        /** @description 事件信件的来源：交给模型时套在事件外面（[`EventSource::raw`]），界面按种类显示。 */
        EventSource: {
            /** Format: int64 */
            job_id: number;
            /** @enum {string} */
            kind: "job";
        } | {
            session_id: components["schemas"]["SessionId"];
            /** @enum {string} */
            kind: "thread";
        } | {
            /** @enum {string} */
            kind: "wake";
        };
        /**
         * @description 哪个脚本出的错。
         * @enum {string}
         */
        FaultSource: "precheck" | "stream";
        FeaturesOut: {
            /** @description 配置了 [transcription]：界面显示语音输入 */
            transcription: boolean;
        };
        FieldRow: {
            label: string;
            value: string;
        };
        FileOut: {
            id: string;
            name: string;
            media_type: components["schemas"]["MediaType"];
            /** Format: int64 */
            size: number;
            url: string;
            /** @description 能内联显示的图片；其余是下载链接 */
            inline: boolean;
            /** Format: date-time */
            created_at: string;
        };
        /** @enum {string} */
        FireWhen: "output" | "changed";
        FiredOut: {
            /** @description 是否投出了信（前置检查判定这次不触发时为 false） */
            fired: boolean;
            session_id: components["schemas"]["SessionId"] | null;
        };
        /** @description 时间线上发生的一件事。 */
        Happening: {
            /** @enum {string} */
            kind: "created";
        } | {
            body: string;
            /** @enum {string} */
            kind: "comment";
        } | {
            /** @enum {string} */
            kind: "description";
        } | {
            change: components["schemas"]["Change_String"];
            /** @enum {string} */
            kind: "title";
        } | {
            change: components["schemas"]["Change_String"];
            /** @enum {string} */
            kind: "status";
        } | {
            change: components["schemas"]["Change_Priority"];
            /** @enum {string} */
            kind: "priority";
        } | {
            change: components["schemas"]["Change_Option_Assignee"];
            /** @enum {string} */
            kind: "assignee";
        } | {
            change: components["schemas"]["Change_Option_AgentId"];
            /** @enum {string} */
            kind: "owner";
        } | {
            change: components["schemas"]["Change_Vec_i64"];
            /** @enum {string} */
            kind: "depends_on";
        } | {
            change: components["schemas"]["DependencyChange"];
            /** @enum {string} */
            kind: "dependency";
        } | {
            body: string;
            /** @enum {string} */
            kind: "run_failed";
        } | {
            /** @enum {string} */
            kind: "unsettled";
        } | {
            change: components["schemas"]["Arrival"];
            /** @enum {string} */
            kind: "stopped";
        };
        /**
         * @description 连上就发一次、之后每隔一段时间发一次：前端据此判断连接是否还活着（间隔随心跳下发，手机切后台、代理链路断开时
         *     EventSource 往往收不到错误，只能靠「多久没收到消息」发现），并比较前端构建的版本（手机主屏应用常驻内存，升级后提示刷新）。
         */
        HeartbeatEvent: {
            /** Format: double */
            interval_seconds: number;
            /** @description 服务端当前前端构建的版本；没有前端构建时为 null */
            ui_version: string | null;
        };
        /**
         * Format: int64
         * @description 重要程度：1 日常、2 值得注意、3 重大。
         */
        Importance: number;
        /** @description 人与 agent 协作的一件事（读模型，也是 HTTP 返回的形状）。 */
        Issue: {
            /** Format: int64 */
            id: number;
            title: string;
            description: string;
            status: components["schemas"]["Status"];
            priority: components["schemas"]["Priority"];
            owner: components["schemas"]["AgentId"] | null;
            assignee: components["schemas"]["Assignee"] | null;
            /** @description 创建者：agent id；用户创建的为 `user` */
            creator: components["schemas"]["Actor"];
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            updated_at: string;
        };
        IssueCreate: {
            title: string;
            /** @description 负责的项目（agent id），必填 */
            owner: components["schemas"]["AgentId"];
            description?: string;
            status?: components["schemas"]["Status"];
            priority?: components["schemas"]["Priority"];
            assignee?: components["schemas"]["Assignee"] | null;
            /** @description 依赖的上游 issue id；不能成环 */
            depends_on?: number[];
        };
        IssueDetail: {
            issue: components["schemas"]["BoardIssue"];
            activities: components["schemas"]["Activity"][];
        };
        /** @description 只修改给出的字段。assignee 给出 null 表示取消指派，不出现表示不变。 */
        IssuePatch: {
            owner?: components["schemas"]["AgentId"] | null;
            title?: string | null;
            description?: string | null;
            status?: components["schemas"]["Status"] | null;
            priority?: components["schemas"]["Priority"] | null;
            assignee?: components["schemas"]["Assignee"] | null;
            /** @description 依赖的上游 issue id，整体替换（[] 去掉全部）；不出现表示不变 */
            depends_on?: number[] | null;
        };
        /** @description 谁在这个 issue 上做事、它在等谁（从会话领域与依赖图派生，不存）。 */
        IssueWork: {
            /** @description 每个 agent 在这个 issue 上的会话：agent id -> 会话 id */
            sessions: {
                [key: string]: components["schemas"]["SessionId"];
            };
            assignee_state: components["schemas"]["WorkState"] | null;
            /** @description in_progress、指派给 agent，它的 issue 会话却空闲或不存在：该有人做却没人在做 */
            stalled: boolean;
            /** @description 它依赖的上游、依赖它的下游、还没完成（不是 done / cancelled）的上游：非空时还轮不到它 */
            depends_on: number[];
            blocks: number[];
            waiting_on: number[];
            /** @description 已结束（done / cancelled）：不再挡着依赖它的 issue */
            finished: boolean;
            /** @description 用户能审核（通过或打回）：指派给用户、还没结束 */
            reviewable: boolean;
            /** @description 用户能一键开工（指派给 owner 并进入 in_progress）：还在 backlog、有 owner */
            startable: boolean;
        };
        /**
         * @description 一条展示条目：每种只带自己的字段。U 是用户消息条目：会话页是 [`UserItem`]，热历史的时间线另带发送方
         *     （[`crate::recall::SentUser`]）。
         */
        Item: ({
            text: string;
            attachments: components["schemas"]["Attachment"][];
        } & {
            /** @enum {string} */
            type: "user";
        }) | (components["schemas"]["EventItem"] & {
            /** @enum {string} */
            type: "event";
        }) | {
            text: string;
            /** @enum {string} */
            type: "assistant";
        } | {
            text: string;
            /** @enum {string} */
            type: "thinking";
        } | (components["schemas"]["CallItem"] & {
            /** @enum {string} */
            type: "tool_call";
        }) | (components["schemas"]["ResultItem"] & {
            /** @enum {string} */
            type: "tool_result";
        }) | (components["schemas"]["ErrorItem"] & {
            /** @enum {string} */
            type: "tool_error";
        }) | {
            /** @enum {string} */
            type: "stopped";
        };
        /** @description 在等的后台任务的详情（与 job_status 同一份）。 */
        JobOut: {
            command: string;
            /** @description 取等待项与取任务信息不在同一事务，之间任务可能已经结束（结局还没交回） */
            status: components["schemas"]["JobStatus"];
            /** @description 日志，相对 agent 工作目录 */
            log_path: string;
            /** @description 日志最后几行 */
            tail: string;
        };
        /**
         * @description 后台任务的状态（界面与 job_status 看到的）。
         * @enum {string}
         */
        JobStatus: "running" | "exited" | "killed" | "interrupted";
        KindInDoc: {
            config: components["schemas"]["ScheduleConfig"];
            /** @enum {string} */
            kind: "schedule";
        } | {
            config: components["schemas"]["OnceConfig"];
            /** @enum {string} */
            kind: "once";
        } | {
            config: components["schemas"]["StreamConfig"];
            /** @enum {string} */
            kind: "stream";
        };
        /** @description config 与 status（这种方式自己给出的运行状态，不含凭证；读不出时为 null，原因在 status_error）按 kind 区分。 */
        KindOutDoc: {
            config: components["schemas"]["ScheduleConfig"];
            status: components["schemas"]["ScheduleStatus"] | null;
            /** @enum {string} */
            kind: "schedule";
        } | {
            config: components["schemas"]["OnceConfig"];
            status: components["schemas"]["OnceStatus"] | null;
            /** @enum {string} */
            kind: "once";
        } | {
            config: components["schemas"]["StreamConfig"];
            status: components["schemas"]["StreamStatus"] | null;
            /** @enum {string} */
            kind: "stream";
        };
        Link: {
            title: string;
            url: string;
            summary: string | null;
        };
        LiveSegment: {
            segment_id: string;
            /**
             * Format: int64
             * @description 这一段开始时会话最后一条已保存消息的 id：这之后保存的消息由这一段的事件展示，历史里先不显示
             */
            after_message_id: number | null;
            events: components["schemas"]["RunEventEnvelope"][];
        };
        LoginIn: {
            password: string;
        };
        /**
         * Format: int64
         * @description 一封信件的编号（mail 表的行号）
         */
        MailId: number;
        MailKindOut: {
            /** @enum {string} */
            kind: "message";
        } | {
            /** @description 事件的来源 */
            source: components["schemas"]["EventSource"];
            /** @description 事件是运行中检查（工作还在进行） */
            check_in: boolean;
            /** @enum {string} */
            kind: "event";
        } | {
            /** @description tool_call_id -> 那次挂起调用的结果（用户的回答，或被问 agent 的结局） */
            answers: {
                [key: string]: components["schemas"]["CallResult"];
            };
            /** @enum {string} */
            kind: "answer";
        };
        /** @description 会话信箱里的一封信件（显示在历史之后：排队中的，以及最近一次停止丢弃的）。 */
        MailOut: components["schemas"]["MailKindOut"] & {
            id: components["schemas"]["MailId"];
            content: string;
            attachments: components["schemas"]["FileOut"][];
            state: components["schemas"]["MailState"];
            /** Format: date-time */
            created_at: string;
        };
        /** @enum {string} */
        MailState: "queued" | "consumed" | "discarded";
        MarkReadIn: {
            /**
             * Format: int64
             * @description 用户看到的最新一条；之后才到的保持未读
             */
            through_id: number;
        };
        McpAssetOut: {
            name: string;
            owner: components["schemas"]["AgentId"] | null;
            description: string;
            source: components["schemas"]["AssetSourceOut"];
            editable: boolean;
            transport: components["schemas"]["McpTransport"];
            enabled: boolean;
        };
        McpConfigOut: {
            transport: components["schemas"]["McpTransport"];
            command: string | null;
            args: string[];
            url: string | null;
            /** @description env / headers 里的 secret 只以 ${vault:KEY} 引用形式存储，可以原样返回 */
            env: {
                [key: string]: string;
            };
            headers: {
                [key: string]: string;
            };
            enabled: boolean;
        };
        McpIn: {
            owner?: components["schemas"]["AgentId"] | null;
            transport: components["schemas"]["McpTransport"];
            command?: string | null;
            args?: string[];
            url?: string | null;
            env?: {
                [key: string]: string;
            };
            headers?: {
                [key: string]: string;
            };
            /** @description 先连接一次验证（按用户能用的 secret 解析引用），连不上 422、不保存 */
            probe?: boolean;
        };
        McpOut: components["schemas"]["McpConfigOut"] & {
            owner: components["schemas"]["AgentId"] | null;
            name: string;
            /** @description 新增时连接验证列出的工具（不验证时为空） */
            tools: string[];
        };
        /** @enum {string} */
        McpTransport: "stdio" | "http";
        /** @description 媒体类型：只存 `type/subtype`（小写）。参数（如 charset）不影响怎么显示和交给模型，下载一律按原始字节。 */
        MediaType: string;
        MemoryIn: {
            content: string;
            /** @description 打开编辑时的版本（MemoryOut.version），新建为 null；期间被别处改过、删掉或抢先建了时 409 */
            version?: string | null;
        };
        MemoryOut: {
            name: string;
            owner: components["schemas"]["AgentId"] | null;
            content: string;
            /** Format: date-time */
            updated_at: string;
            path: string;
            /** @description 每一块的上限 */
            max_chars: number;
            /** @description content 的版本：保存时交回 */
            version: string;
        };
        /** @description 一条通知（读模型）。 */
        Notification: {
            /** Format: int64 */
            id: number;
            title: string;
            body: string;
            session_id: components["schemas"]["SessionId"] | null;
            /** Format: int64 */
            issue_id: number | null;
            /** @description 点开打开的页面（推送与通知页共用）；系统通知为 None */
            url: string | null;
            unread: boolean;
            /** Format: date-time */
            created_at: string;
        };
        /** @description 有新通知了（SSE 事件名 `notification`）：前端刷新通知页与未读数；正看着这个会话时把它标为已读。 */
        NotificationEvent: {
            /** Format: int64 */
            notification_id: number;
            session_id: components["schemas"]["SessionId"] | null;
        };
        OnceConfig: {
            at: string;
        };
        OnceStatus: {
            run_at: string;
        };
        OwnerIn: {
            owner?: components["schemas"]["AgentId"] | null;
        };
        /** @description 一个前置检查（存在触发器上，也是工具与接口的参数）。 */
        Precheck: {
            /** @description bash command run before each firing. */
            command: string;
            /** @description Vault keys exposed to the command as env vars. */
            secrets?: string[];
            /**
             * @description 'output': fire when stdout is non-empty. 'changed': fire when stdout differs from the last run (the first run
             *     only records it).
             */
            fire_when?: components["schemas"]["FireWhen"];
        };
        /** @enum {string} */
        Priority: "none" | "urgent" | "high" | "medium" | "low";
        PushKeys: {
            p256dh: string;
            auth: string;
        };
        /** @description 浏览器给的推送订阅（PushSubscription.toJSON() 的 endpoint 与 keys）。 */
        PushSubscriptionIn: {
            endpoint: string;
            keys: components["schemas"]["PushKeys"];
        };
        Question: {
            /** @description The full question. */
            question: string;
            /** @description Very short label, e.g. 'Timeframe'. */
            header: string;
            options: components["schemas"]["QuestionOption"][];
            /** @description Allow choosing several options. */
            multi_select: boolean;
        };
        /** @description 用户对一个问题的回答：选中的选项，和/或「其他」里填的文字。 */
        QuestionAnswer: {
            selected?: string[];
            other?: string | null;
        };
        QuestionOption: {
            /** @description Short choice text shown on the button. */
            label: string;
            /** @description What choosing it means, if not obvious. */
            description: string;
        };
        /** @description 一份配置能用（use）的内置工具与资产（skill、MCP server、secret、记忆）。 */
        ReachOut: {
            tools: components["schemas"]["ToolOut"][];
            assets: components["schemas"]["UsableAsset"][];
        };
        /** @description 库里的一件事。 */
        Record: {
            /** Format: int64 */
            id: number;
            agent_id: components["schemas"]["AgentId"];
            /** Format: date */
            day: string;
            title: string;
            summary: string;
            refs: string[];
            importance: components["schemas"]["Importance"];
            /** Format: date-time */
            created_at: string;
        };
        RejectIn: {
            /** @description 打回的理由，记成评论 */
            reason: string;
        };
        /** @description 用户在会话里发来的一件事，按种类分开。 */
        ReplyIn: {
            prompt: string;
            /** @description 先经 POST /api/files 上传得到的 id */
            attachments?: string[];
            /** @enum {string} */
            kind: "message";
        } | {
            call_id: string;
            answers: components["schemas"]["QuestionAnswer"][];
            /** @enum {string} */
            kind: "answer";
        } | {
            call_id: string;
            choice: components["schemas"]["Choice"];
            /** @enum {string} */
            kind: "approval";
        };
        ResultItem: {
            tool_name: string;
            tool_call_id: string;
            /** @description 交给模型的文字（各文字段以换行连接） */
            text: string;
            /** @description 工具结果挂上的文件（TOOL_ATTACHMENTS） */
            attachments: components["schemas"]["Attachment"][];
            /** @description 工具交回给模型的图片（如浏览器截图）：属于这次调用的过程，不是给用户的文件 */
            images: components["schemas"]["Attachment"][];
            /** @description 结果按工具自己的展示规则画成的展示块；None 显示原文（工具没定规则、结果不合它的类型、事后补的结果、带图片的结果） */
            view: components["schemas"]["ViewBlock"][] | null;
            /**
             * Format: date-time
             * @description 工具结束的时间；事后补的结果、没记结束时间的结果为 None
             */
            at: string | null;
        };
        /** @description 运行出了暂时性的错、在等下一次尝试（只在内存里，重启后从上一步续跑、不再有它）：会话页显示错误与下次尝试的时间。 */
        Retrying: {
            error: string;
            /** Format: date-time */
            next_at: string;
        };
        RoleIn: {
            description: string;
            allow: string[];
            deny: string[];
        };
        RoleOut: {
            name: string;
            description: string;
            /** @description 内置角色（base、admin）代码里固定的规则，不可改 */
            fixed_allow: string[];
            /** @description 可编辑的规则 */
            allow: string[];
            deny: string[];
            /** @description 内置角色不能删 */
            builtin: boolean;
            /** @description 能写进 agent 的 roles：base 每个 agent 隐含，不能选 */
            assignable: boolean;
            /** @description 只按这个角色的规则（固定的与可编辑的）能用的 */
            reach: components["schemas"]["ReachOut"];
            /** @description 带这个角色的 agent 的 id（含归档的）；base 是全部 agent */
            agents: components["schemas"]["AgentId"][];
        };
        RolePreviewIn: {
            /** @description 已有的角色（内置的加上固定规则）；null 为还没建的 */
            name?: string | null;
            allow: string[];
            deny: string[];
        };
        /** @description 只改给出的字段（缺省或 null 是不变）：界面只提交用户改过的，编辑期间 agent 改了别的字段不会被旧值盖掉。 */
        RoleUpdate: {
            description?: string | null;
            allow?: string[] | null;
            deny?: string[] | null;
        };
        /** @description 一条运行事件。 */
        RunEvent: {
            item: components["schemas"]["Item"];
            /** @enum {string} */
            event: "item";
        } | {
            item_type: components["schemas"]["DeltaType"];
            text: string;
            /** @enum {string} */
            event: "delta";
        };
        /** @description 推给前端的一条（SSE 事件名 `run`）。 */
        RunEventEnvelope: components["schemas"]["RunEvent"] & {
            session_id: components["schemas"]["SessionId"];
            agent: string;
            segment_id: string;
            seq: number;
        };
        /**
         * Format: int64
         * @description 一次运行的编号（runs 表的行号）
         */
        RunId: number;
        RunRecord: {
            id: components["schemas"]["RunId"];
            session_id: components["schemas"]["SessionId"];
            /** Format: date-time */
            started_at: string;
            state: components["schemas"]["RunState"];
        };
        /** @description 运行到了哪一步。 */
        RunState: {
            /** @enum {string} */
            outcome: "unfinished";
        } | {
            /** Format: date-time */
            ended_at: string;
            /** @enum {string} */
            outcome: "done";
        } | {
            /** Format: date-time */
            ended_at: string;
            /** @enum {string} */
            outcome: "stopped";
        } | {
            /** Format: date-time */
            ended_at: string;
            error: string;
            /** @enum {string} */
            outcome: "failed";
        };
        /**
         * @description 这个会话的历史或运行状态变了（SSE 事件名 `saved`）：运行中又保存了一步（实时事件换成新的一段），或运行记录变了
         *     （运行或压缩开始、结束、失败、停止、重试）。会话页只按它重新拉取：整份会话可能有几 MB，不随全局的 changed 拉。
         */
        SavedEvent: {
            session_id: components["schemas"]["SessionId"];
        };
        /** @description 历史里的一条：来源消息的数据库 id（用于定位压缩点）与条目。 */
        SavedItem: {
            /** Format: int64 */
            message_id: number;
            item: components["schemas"]["Item"];
        };
        ScheduleConfig: {
            cron: string;
        };
        /** @description 界面与模型看到的状态。 */
        ScheduleStatus: {
            next_run_at: string;
            skipped: components["schemas"]["SkippedFires"] | null;
        };
        ScriptGrantOut: {
            /** Format: int64 */
            id: number;
            agent_id: components["schemas"]["AgentId"];
            skill: string;
            script: string;
            args: string[];
            secrets: string[];
            /** @description 用户点「始终允许」的那次调用所在的会话（可能已删除） */
            session_id: components["schemas"]["SessionId"];
            /** Format: date-time */
            created_at: string;
            /**
             * Format: int64
             * @description 按它直接放行的次数与最后一次的时间
             */
            hits: number;
            /** Format: date-time */
            last_used_at: string | null;
        };
        SecretIn: {
            owner?: components["schemas"]["AgentId"] | null;
            value: string;
            description?: string;
            /** @description null：已有的保持原值，新建的允许 */
            allow_shell?: boolean | null;
        };
        SecretOut: {
            owner: components["schemas"]["AgentId"] | null;
            name: string;
            description: string;
            allow_shell: boolean;
            /** Format: date-time */
            updated_at: string;
        };
        /** @description SSE 里每条 data 的形状（按 type 区分）。只用于接口描述：发出的是总线上各领域序列化好的事件加上 type。 */
        ServerEvent: (components["schemas"]["RunEventEnvelope"] & {
            /** @enum {string} */
            type: "run";
        }) | (components["schemas"]["SavedEvent"] & {
            /** @enum {string} */
            type: "saved";
        }) | (components["schemas"]["NotificationEvent"] & {
            /** @enum {string} */
            type: "notification";
        }) | (components["schemas"]["ChangedEvent"] & {
            /** @enum {string} */
            type: "changed";
        }) | (components["schemas"]["HeartbeatEvent"] & {
            /** @enum {string} */
            type: "heartbeat";
        });
        SessionDetail: {
            session: components["schemas"]["SessionOut"];
            /** @description 完整原始历史的展示条目（正在进行的那一段由 live 展示） */
            items: components["schemas"]["SavedItem"][];
            /** @description 压缩点：through_message_id 及之前的消息已由 summary 代替发给模型（原文仍完整展示） */
            compactions: components["schemas"]["CompactionOut"][];
            /** @description 显示在历史之后的信件：排队中的（处理中的用户消息、已提交的回答），以及最近一次停止丢弃的 */
            mail: components["schemas"]["MailOut"][];
            run: components["schemas"]["RunRecord"] | null;
            /** @description 这个会话开出的 thread */
            threads: components["schemas"]["ThreadOut"][];
            live: components["schemas"]["LiveSegment"] | null;
            retrying: components["schemas"]["Retrying"] | null;
            /** @description 工作状态（与会话列表同一条规则）：不是 idle 前端就显示停止 */
            state: components["schemas"]["WorkState"];
            /** @description 在等的东西：后台任务、委派、到点唤醒、提问、等批准 */
            waits: components["schemas"]["WaitOut"][];
            context: components["schemas"]["ContextOut"];
            /** @description 这个会话名下全部运行的累计用量；一次运行的用量在每次模型回复后记上 */
            usage: components["schemas"]["UsageTotals"];
        };
        /** @description 会话的编号 */
        SessionId: string & {
            readonly __brand: 'SessionId';
        };
        /** @description 会话列表里的一行：多一个只在列表里算的工作状态（会话页看 SessionDetail.state）。 */
        SessionListItem: components["schemas"]["SessionOut"] & {
            state: components["schemas"]["WorkState"];
        };
        /** @description 会话的来源与发起方（界面按 `kind` 区分）。委派开出的是 thread，带 thread 名与发起它的父会话（父会话删除后为空）。 */
        SessionOrigin: {
            /** @enum {string} */
            kind: "user";
        } | {
            thread: string;
            parent_session_id: components["schemas"]["SessionId"] | null;
            /** @enum {string} */
            kind: "delegation";
        } | {
            /** Format: int64 */
            trigger_id: number;
            /** @enum {string} */
            kind: "trigger";
        } | {
            /** Format: int64 */
            issue_id: number;
            /** @enum {string} */
            kind: "issue";
        };
        SessionOut: {
            id: components["schemas"]["SessionId"];
            agent_id: components["schemas"]["AgentId"];
            title: string;
            /** @description 会话的来源与发起方（结构化，前端不认识库里的编号格式）；委派开出的 thread 带名字与父会话 */
            origin: components["schemas"]["SessionOrigin"];
            /** @description 有指向它的未读通知（用户在等的回答、提问、agent 主动通知、运行失败）：未读不另存，从通知派生 */
            unread: boolean;
            /** Format: date-time */
            created_at: string;
            /** Format: date-time */
            updated_at: string;
        };
        /** @description 挂给用户的一个文件（存储形态）：文件存储里的哪个文件，以及展示要用的名字与类型。地址、能否内联由它派生（[`Attachment`]）。 */
        SharedFile: {
            file_id: string;
            name: string;
            media_type: components["schemas"]["MediaType"];
        };
        SitePageOut: {
            agent_id: components["schemas"]["AgentId"];
            /** @description 页面目录名（site/<name>/） */
            name: string;
            /** @description index.html 的 <title>，没有就是 name */
            title: string;
            /**
             * Format: date-time
             * @description 页面目录里最近改动的文件的时间（数据文件刷新也算）
             */
            modified: string;
            /** @description 打开页面的签名地址（以 / 结尾，页面里的相对路径据此解析） */
            url: string;
        };
        SkillDetail: {
            name: string;
            description: string;
            owner: components["schemas"]["AgentId"] | null;
            body: string;
            /** @description SKILL.md 的版本：保存时交回 */
            version: string;
        };
        SkillIn: {
            owner?: components["schemas"]["AgentId"] | null;
            description: string;
            body: string;
            /** @description 打开编辑时的版本（SkillDetail.version），新建为 null；期间被别处改过、删掉或抢先建了时 409 */
            version?: string | null;
        };
        SkillOut: {
            name: string;
            description: string;
            owner: components["schemas"]["AgentId"] | null;
        };
        /** @description 连续跳过（界面与模型看的部分）。 */
        SkippedFires: {
            /**
             * Format: int32
             * @description 连续跳过了几次
             */
            count: number;
            /**
             * Format: date-time
             * @description 第一次跳过的时间
             */
            since: string;
            /** @description 最近一次跳过时挡路的会话在运行还是在等待 */
            previous_state: components["schemas"]["Busy"];
        };
        StartIn: {
            agent_id: components["schemas"]["AgentId"];
            prompt: string;
            /** @description 先经 POST /api/files 上传得到的 id */
            attachments?: string[];
        };
        /** @enum {string} */
        Status: "backlog" | "in_progress" | "in_review" | "blocked" | "done" | "cancelled";
        /**
         * @description 存成文件的四种：可以新建、改、删、转移 owner（记忆不转移）。
         * @enum {string}
         */
        StoredKind: "skill" | "mcp" | "secret" | "memory";
        StreamConfig: {
            command: string;
            secrets?: string[];
            /** Format: double */
            debounce_seconds?: number;
        };
        StreamStatus: {
            /**
             * Format: date-time
             * @description 当前进程的启动时间；没有在运行（停用、刚启用或改配置后还没起来、退出后在等重启）时为 null
             */
            running_since: string | null;
            /**
             * Format: int32
             * @description 连续快速失败的次数（达到告警次数时原因在触发器的 faults 里）
             */
            failures: number;
            /** @description stderr 写到哪个文件（相对该 agent 的工作目录） */
            stderr_log: string;
        };
        /**
         * @description 思考档位：default 不指定档位，按模型与服务端的默认；off 关闭；其余是 effort 档位。怎么换算由模型层决定。
         * @enum {string}
         */
        Thinking: "default" | "off" | "low" | "medium" | "high" | "xhigh";
        /** @description 会话开出的一个 thread：子会话与它的名字（与 origin 里的同一个，列 thread 时不用再按来源收窄）。 */
        ThreadOut: components["schemas"]["SessionOut"] & {
            thread: string;
        };
        /** @description 一个内置工具（当前配置下注册的，工具自己声明读写与是否管理类），在规则里写作 `tool:<name>`。 */
        ToolOut: {
            name: string;
            access: components["schemas"]["Access"];
            /** @description 管理类工具：base 角色没有 */
            admin: boolean;
        };
        TranscriptOut: {
            /** @description 听不到人说话时为空字符串 */
            text: string;
        };
        /** @description 新建触发器：kind 与 config 按方式区分（有默认值的配置项可以不填），config 原样交给触发方式校验、补默认值。 */
        TriggerCreate: components["schemas"]["KindInDoc"] & {
            name: string;
            agent_id: components["schemas"]["AgentId"];
            prompt: string;
            precheck?: components["schemas"]["Precheck"] | null;
        };
        /** @description 一条出错：来源、原因。 */
        TriggerFault: {
            source: components["schemas"]["FaultSource"];
            error: string;
        };
        TriggerKindOut: {
            name: string;
            /** @description 什么时候触发、config 的格式 */
            description: string;
        };
        /** @description 一个触发器：各方式共有的字段，加上按 kind 区分的 config 与 status。 */
        TriggerOut: components["schemas"]["KindOutDoc"] & {
            /** Format: int64 */
            id: number;
            name: string;
            agent_id: components["schemas"]["AgentId"];
            prompt: string;
            enabled: boolean;
            precheck: components["schemas"]["Precheck"] | null;
            /** Format: date-time */
            last_checked_at: string | null;
            /** Format: date-time */
            last_fired_at: string | null;
            last_session_id: components["schemas"]["SessionId"] | null;
            /** @description 被自动停用的原因（重新启用时清除） */
            disabled_reason: string | null;
            /** @description 脚本出错：前置检查失败、流式命令反复退出 */
            faults: components["schemas"]["TriggerFault"][];
            /**
             * Format: date-time
             * @description 结束的时间：不为空表示它不会再触发了（一次性触发器跑完），只能查看与删除
             */
            finished_at: string | null;
            /** @description 存下的运行状态坏了、读不出时的原因（这时 status 为 null） */
            status_error: string | null;
        };
        /** @description 只修改给出的字段。precheck 给出 null 表示去掉前置检查，不出现表示不变。 */
        TriggerPatch: {
            prompt?: string | null;
            /** @description 修改请求不带 kind：按已存的 kind 校验，格式同新建时这种方式的 config */
            config?: Record<string, unknown> | null;
            enabled?: boolean | null;
            precheck?: components["schemas"]["Precheck"] | null;
        };
        UnreadOut: {
            /** Format: int64 */
            count: number;
        };
        /** @description 能用的一项资产（存成文件的那四种）。 */
        UsableAsset: {
            kind: components["schemas"]["StoredKind"];
            name: string;
        };
        /** @description 一个分组的汇总。没有选中的维度为 None（例如只按模型分组时 agent_id 与 day 都是 None）。 */
        UsageBucket: components["schemas"]["UsageTotals"] & {
            agent_id: components["schemas"]["AgentId"] | null;
            model: string | null;
            /**
             * Format: date
             * @description 配置时区里的日历日
             */
            day: string | null;
        };
        /**
         * @description 用量的各项计数（一笔的，或一组的和）。`input_tokens` 是输入总数，**已经包含**缓存读写两项，展示为「总数与其中两部分」，
         *     不要相加。
         */
        UsageTotals: {
            /** Format: int64 */
            requests: number;
            /** Format: int64 */
            input_tokens: number;
            /** Format: int64 */
            output_tokens: number;
            /** Format: int64 */
            cache_read_tokens: number;
            /** Format: int64 */
            cache_write_tokens: number;
        };
        VapidOut: {
            public_key: string;
        };
        /** @description 一个展示块。输出时字段一律都在（没有就是 null），前端类型与实际输出一致。 */
        ViewBlock: {
            command: string | null;
            output: string | null;
            status: string | null;
            failed: boolean;
            /** @enum {string} */
            kind: "terminal";
        } | {
            title: string | null;
            text: string;
            /** @enum {string} */
            kind: "code";
        } | {
            title: string | null;
            text: string;
            /** @enum {string} */
            kind: "diff";
        } | {
            title: string | null;
            text: string;
            /** @enum {string} */
            kind: "markdown";
        } | {
            rows: components["schemas"]["FieldRow"][];
            /** @enum {string} */
            kind: "fields";
        } | {
            columns: string[];
            rows: string[][];
            /** @enum {string} */
            kind: "table";
        } | {
            links: components["schemas"]["Link"][];
            /** @enum {string} */
            kind: "links";
        };
        /** @description 会话在等的一项东西（会话领域的 [`Wait`]）；后台任务另带任务的详情。 */
        WaitOut: {
            /** Format: int64 */
            job_id: number;
            /** Format: date-time */
            since: string;
            job: components["schemas"]["JobOut"];
            /** @enum {string} */
            kind: "job";
        } | {
            thread_session_id: components["schemas"]["SessionId"];
            /** Format: double */
            check_seconds: number | null;
            /** Format: date-time */
            since: string;
            /** @enum {string} */
            kind: "delegation";
        } | {
            /** Format: date-time */
            due_at: string;
            note: string;
            /** Format: date-time */
            since: string;
            /** @enum {string} */
            kind: "wake";
        } | {
            call_id: string;
            headers: string[];
            /** @enum {string} */
            kind: "ask";
        } | {
            call_id: string;
            tool_name: string;
            /** @enum {string} */
            kind: "approval";
        };
        /** @enum {string} */
        WorkState: "running" | "waiting" | "idle";
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    login: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LoginIn"];
            };
        };
        responses: {
            /** @description 已登录 */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    logout: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 已登出 */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    me: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 已登录 */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    events: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 事件流（text/event-stream），每条 data 是一个 ServerEvent */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "text/event-stream": components["schemas"]["ServerEvent"];
                };
            };
        };
    };
    agent_defaults: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentDefaults"];
                };
            };
        };
    };
    preview_agent: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AgentPreviewIn"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ReachOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    list_roles: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoleOut"][];
                };
            };
        };
    };
    preview_role: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RolePreviewIn"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ReachOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    create_role: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RoleIn"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoleOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    delete_role: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    update_role: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RoleUpdate"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoleOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    list_agents: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentOut"][];
                };
            };
        };
    };
    create_agent: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AgentCreate"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    get_agent: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description agent 的编号 */
                agent_id: components["schemas"]["AgentId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    delete_agent: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description agent 的编号 */
                agent_id: components["schemas"]["AgentId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    update_agent: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description agent 的编号 */
                agent_id: components["schemas"]["AgentId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AgentUpdate"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    agent_assets: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description agent 的编号 */
                agent_id: components["schemas"]["AgentId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentAssetsOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    set_default_agent: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description agent 的编号 */
                agent_id: components["schemas"]["AgentId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    unset_default_agent: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description agent 的编号 */
                agent_id: components["schemas"]["AgentId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    archive_agent: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description agent 的编号 */
                agent_id: components["schemas"]["AgentId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    unarchive_agent: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description agent 的编号 */
                agent_id: components["schemas"]["AgentId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    get_private_memory: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description agent 的编号 */
                agent_id: components["schemas"]["AgentId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MemoryOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    put_private_memory: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description agent 的编号 */
                agent_id: components["schemas"]["AgentId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MemoryIn"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MemoryOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    list_assets: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CatalogAssetOut"][];
                };
            };
        };
    };
    transfer_asset: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description skill、mcp、secret 或 memory */
                kind: string;
                name: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["OwnerIn"];
            };
        };
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    get_memory: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MemoryOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    put_memory: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MemoryIn"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MemoryOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    delete_memory: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    get_skill: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SkillDetail"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    put_skill: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SkillIn"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SkillOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    delete_skill: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    list_skill_files: {
        parameters: {
            query?: {
                /** @description 相对目录根的路径，空为根 */
                path?: string;
            };
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["EntryOut"][];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    read_skill_file: {
        parameters: {
            query?: {
                /** @description 相对目录根的路径，空为根 */
                path?: string;
            };
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 文件内容 */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    get_mcp: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["McpOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    add_mcp: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["McpIn"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["McpOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    delete_mcp: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    set_mcp_enabled: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["EnabledIn"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["McpOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    probe_mcp: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": string[];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    get_secret: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SecretOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    set_secret: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SecretIn"];
            };
        };
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    delete_secret: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    list_providers: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": string[];
                };
            };
        };
    };
    list_provider_models: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": string[];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            502: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    features: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FeaturesOut"];
                };
            };
        };
    };
    list_sessions: {
        parameters: {
            query?: {
                agent_id?: components["schemas"]["AgentId"];
                /** @description 只列这些来源的会话（可以重复给） */
                origin?: string[];
                /** @description 只列这个触发器发起的会话（不能再给 origin） */
                trigger_id?: number;
                /** @description 关键字：只列标题或消息含有它的会话（不区分 ASCII 大小写；不能为空串） */
                q?: string;
                /** @description 1–200，默认 50 */
                limit?: number;
                offset?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionListItem"][];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    start_session: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["StartIn"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    get_session: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description 会话的编号 */
                session_id: components["schemas"]["SessionId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionDetail"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    delete_session: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description 会话的编号 */
                session_id: components["schemas"]["SessionId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    reply: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description 会话的编号 */
                session_id: components["schemas"]["SessionId"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ReplyIn"];
            };
        };
        responses: {
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    stop: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description 会话的编号 */
                session_id: components["schemas"]["SessionId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    compact: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description 会话的编号 */
                session_id: components["schemas"]["SessionId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    retry: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description 会话的编号 */
                session_id: components["schemas"]["SessionId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    mark_session_read: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description 会话的编号 */
                session_id: components["schemas"]["SessionId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    upload: {
        parameters: {
            query: {
                /** @description 文件名 */
                name: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** @description 文件内容（任意类型） */
        requestBody: {
            content: {
                "application/octet-stream": string;
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FileOut"];
                };
            };
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    download: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                file_id: string;
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 文件内容 */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    list_workspace: {
        parameters: {
            query?: {
                /** @description 相对目录根的路径，空为根 */
                path?: string;
            };
            header?: never;
            path: {
                /** @description agent 的编号 */
                agent_id: components["schemas"]["AgentId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["EntryOut"][];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    read_workspace_file: {
        parameters: {
            query?: {
                /** @description 相对目录根的路径，空为根 */
                path?: string;
            };
            header?: never;
            path: {
                /** @description agent 的编号 */
                agent_id: components["schemas"]["AgentId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description 文件内容 */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    list_script_grants: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScriptGrantOut"][];
                };
            };
        };
    };
    revoke_script_grant: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                grant_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    kill_job: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description agent 的编号 */
                agent_id: components["schemas"]["AgentId"];
                job_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    list_issues: {
        parameters: {
            query?: {
                /** @description 只要这些状态（可以重复给）；不给是全部 */
                status?: components["schemas"]["Status"][];
                /** @description 指派人：agent id，或 `user` 表示用户 */
                assignee?: string;
                /** @description 负责项目的 agent id */
                owner?: components["schemas"]["AgentId"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoardIssue"][];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    create_issue: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["IssueCreate"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoardIssue"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    get_issue: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                issue_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["IssueDetail"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    delete_issue: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                issue_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    update_issue: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                issue_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["IssuePatch"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoardIssue"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    comment_issue: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                issue_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CommentIn"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Activity"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    approve_issue: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                issue_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoardIssue"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    start_issue: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                issue_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoardIssue"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    reject_issue: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                issue_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RejectIn"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BoardIssue"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    trigger_kinds: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TriggerKindOut"][];
                };
            };
        };
    };
    list_triggers: {
        parameters: {
            query?: {
                /** @description 列已结束的（跑完的一次性触发器）而不是还会触发的 */
                finished?: boolean;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TriggerOut"][];
                };
            };
        };
    };
    create_trigger: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["TriggerCreate"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TriggerOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    delete_trigger: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                trigger_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    update_trigger: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                trigger_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["TriggerPatch"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TriggerOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    run_trigger: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                trigger_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FiredOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    list_notifications: {
        parameters: {
            query?: {
                /** @description 1–500，默认 100 */
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Notification"][];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    unread: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UnreadOut"];
                };
            };
        };
    };
    mark_notifications_read: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MarkReadIn"];
            };
        };
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    mark_issue_read: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                issue_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    vapid_key: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["VapidOut"];
                };
            };
        };
    };
    subscribe: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PushSubscriptionIn"];
            };
        };
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    test_push: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    usage_summary: {
        parameters: {
            query: {
                /** @description 带时区的时刻（`2026-03-01T00:00:00Z`）：不带时区的时刻没有确定含义，拒绝 */
                since: string;
                until: string;
                /** @description 分组维度（可以重复给）；不给就只有一行总计 */
                group_by?: ("agent" | "model" | "day")[];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UsageBucket"][];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    agent_history: {
        parameters: {
            query?: {
                /** @description 关键词（每个词都要出现在标题、摘要或引用里）；不给按日期倒序 */
                query?: string;
                /** @description 含两端的日期 */
                since?: string;
                until?: string;
                /** @description 1–3，默认 1 */
                min_importance?: components["schemas"]["Importance"];
                /** @description 默认 20，最多 200 */
                limit?: number;
            };
            header?: never;
            path: {
                /** @description agent 的编号 */
                agent_id: components["schemas"]["AgentId"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Record"][];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    transcribe: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** @description 一段语音 */
        requestBody: {
            content: {
                "application/octet-stream": string;
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TranscriptOut"];
                };
            };
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            413: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            502: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    list_pages: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SitePageOut"][];
                };
            };
        };
    };
}
