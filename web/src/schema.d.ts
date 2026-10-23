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
        /** Login */
        post: operations["login_api_auth_login_post"];
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
        /** Logout */
        post: operations["logout_api_auth_logout_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/hooks/{name}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Hook
         * @description 外部事件入口：Authorization: Bearer <token>。不带 session_id 时新建会话；带上之前返回的则在该会话里继续。
         */
        post: operations["hook_api_hooks__name__post"];
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
        /**
         * Agent Defaults
         * @description 新建 agent 时表单预填的默认值（与创建时套用的是同一份 AgentService.defaults）。
         */
        get: operations["agent_defaults_api_agent_defaults_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/permissions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Permissions
         * @description 全部已登记工具的权限，按登记顺序（含 web，即使没配置 [web]）。MCP 工具是 mcp:<server>:<tool>，不在其中。
         */
        get: operations["list_permissions_api_permissions_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/permissions/resolve": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Resolve Permissions
         * @description agent 配置（还没保存的也行）最终能用的工具：public + 角色 + allow / deny，与运行时同一套计算。
         */
        post: operations["resolve_permissions_api_permissions_resolve_post"];
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
        /**
         * List Roles
         * @description 全部角色：内置在前，自建的按名称。
         */
        get: operations["list_roles_api_roles_get"];
        put?: never;
        post?: never;
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
        /**
         * Update Role
         * @description 整体替换自建角色的描述与规则；从引用它的 agent 的下一次运行起生效。
         */
        put: operations["update_role_api_roles__name__put"];
        /** Create Role */
        post: operations["create_role_api_roles__name__post"];
        /**
         * Delete Role
         * @description 有 agent 引用时 409。
         */
        delete: operations["delete_role_api_roles__name__delete"];
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
        /**
         * List Providers
         * @description 配置里的 LLM provider 名，即 agent 模型 "<provider 名>:<模型名>" 的前半部分。
         */
        get: operations["list_providers_api_providers_get"];
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
        /**
         * List Provider Models
         * @description 向该 provider 实时查询的模型名；provider 出错时为 502 并带上原因。
         */
        get: operations["list_provider_models_api_providers__name__models_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Agents */
        get: operations["list_agents_api_agents_get"];
        put?: never;
        /** Create Agent */
        post: operations["create_agent_api_agents_post"];
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
        /** Get Agent */
        get: operations["get_agent_api_agents__agent_id__get"];
        /** Update Agent */
        put: operations["update_agent_api_agents__agent_id__put"];
        post?: never;
        /** Delete Agent */
        delete: operations["delete_agent_api_agents__agent_id__delete"];
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
        /**
         * Set Default Agent
         * @description 设为系统默认 agent（取代原来的默认）。
         */
        put: operations["set_default_agent_api_agents__agent_id__default_put"];
        post?: never;
        /**
         * Unset Default Agent
         * @description 取消它的系统默认身份；它不是默认时不做任何事。
         */
        delete: operations["unset_default_agent_api_agents__agent_id__default_delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/memory": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Get Memory
         * @description owner 为空是全局记忆（所有 agent 共享），否则是该 agent 的记忆。
         */
        get: operations["get_memory_api_memory_get"];
        /** Put Memory */
        put: operations["put_memory_api_memory_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/skills": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Skills
         * @description owner 为空列出公共 skill，否则列出该 agent 的私有 skill。
         */
        get: operations["list_skills_api_skills_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/skills/corrupt": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Corrupt Skills
         * @description owner 名下（缺省为公共）读不出来的 skill，GET /skills 不列它们。
         */
        get: operations["list_corrupt_skills_api_skills_corrupt_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/skill": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Get Skill */
        get: operations["get_skill_api_skill_get"];
        /** Put Skill */
        put: operations["put_skill_api_skill_put"];
        post?: never;
        /** Delete Skill */
        delete: operations["delete_skill_api_skill_delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/skill/package": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * Export Skill
         * @description 下载标准 .skill 文件（zip：`<name>/SKILL.md` 与版本管理里的附件）。
         */
        get: operations["export_skill_api_skill_package_get"];
        put?: never;
        /**
         * Import Skill
         * @description 请求体是一个标准 .skill 文件，导入为 owner（缺省为公共）的 skill，一次提交、作者 user。同名 skill 已存在时 409，
         *     replace=true 覆盖。包的结构不对（路径越界、符号链接、顶层目录不唯一、名字与目录不一致……）422，超限 413。
         */
        post: operations["import_skill_api_skill_package_post"];
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
        /**
         * List Sessions
         * @description 工作状态不是 idle 的会话（在全部符合筛选的会话里，不只是这一页）置顶，其余按最近更新排序。
         */
        get: operations["list_sessions_api_sessions_get"];
        put?: never;
        /** Start Session */
        post: operations["start_session_api_sessions_post"];
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
        /** Get Session */
        get: operations["get_session_api_sessions__session_id__get"];
        put?: never;
        post?: never;
        /** Delete Session */
        delete: operations["delete_session_api_sessions__session_id__delete"];
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
        /** Reply */
        post: operations["reply_api_sessions__session_id__messages_post"];
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
        /**
         * Stop
         * @description 停止这个会话：结束正在进行的运行（已经产生的部分照常保存）、清空信箱，连带停下它发起的异步工作
         *     （docs/architecture.md「停止会话」）。
         */
        post: operations["stop_api_sessions__session_id__stop_post"];
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
         * Compact
         * @description 手动压缩上下文（自动压缩见 sessions.compaction）：排在正在进行与排队的运行之后。
         */
        post: operations["compact_api_sessions__session_id__compact_post"];
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
        /**
         * Mark Read
         * @description 标记会话已读；指向它的通知由通知领域随 SessionRead 标为已读。
         */
        post: operations["mark_read_api_sessions__session_id__read_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/tasks": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** List Tasks */
        get: operations["list_tasks_api_tasks_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/tasks/{task_id}/retry": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Retry Task */
        post: operations["retry_task_api_tasks__task_id__retry_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/mcp": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Mcp
         * @description owner 视角下的 server：它私有的在前，然后是公共的（带 shadowed）；owner 缺省时只列公共 server。
         */
        get: operations["list_mcp_api_mcp_get"];
        put?: never;
        /** Add Mcp */
        post: operations["add_mcp_api_mcp_post"];
        /** Delete Mcp */
        delete: operations["delete_mcp_api_mcp_delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/mcp/corrupt": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Corrupt Mcp
         * @description owner 名下（缺省为公共）配置坏了的 server，GET /mcp 不列它们。
         */
        get: operations["list_corrupt_mcp_api_mcp_corrupt_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/mcp/tools": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Probe Mcp */
        post: operations["probe_mcp_api_mcp_tools_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/mcp/enabled": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Set Mcp Enabled */
        put: operations["set_mcp_enabled_api_mcp_enabled_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/vault": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * List Secrets
         * @description owner 视角下的 secret（不含值）：它私有的在前，然后是公共的（带 shadowed）；owner 缺省时只列公共 secret。
         */
        get: operations["list_secrets_api_vault_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/vault/{key}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /**
         * Set Secret
         * @description owner 缺省为公共 secret，否则为该 agent 私有。
         */
        put: operations["set_secret_api_vault__key__put"];
        post?: never;
        /** Delete Secret */
        delete: operations["delete_secret_api_vault__key__delete"];
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
        /** Vapid Key */
        get: operations["vapid_key_api_push_vapid_get"];
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
        /** Subscribe */
        post: operations["subscribe_api_push_subscriptions_post"];
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
        /** Test Push */
        post: operations["test_push_api_push_test_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/resources/{kind}/history": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /**
         * History
         * @description owner 缺省为公共资源；新的在前，第一条即当前生效的一版。只列最近 50 版，更早的仍保留在仓库里。
         */
        get: operations["history_api_resources__kind__history_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/resources/{kind}/restore": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Restore
         * @description 把某一版的内容作为新的一版写入。
         */
        post: operations["restore_api_resources__kind__restore_post"];
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
        /** Trigger Kinds */
        get: operations["trigger_kinds_api_trigger_kinds_get"];
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
        /** List Triggers */
        get: operations["list_triggers_api_triggers_get"];
        put?: never;
        /** Create Trigger */
        post: operations["create_trigger_api_triggers_post"];
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
        /** Delete Trigger */
        delete: operations["delete_trigger_api_triggers__trigger_id__delete"];
        options?: never;
        head?: never;
        /** Update Trigger */
        patch: operations["update_trigger_api_triggers__trigger_id__patch"];
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
         * Run Trigger
         * @description 立即触发一次（不带事件内容）。有前置检查时同样先跑检查，可以用来测试整条链路。
         *
         *     不消耗一次性触发器预定的那一次；已归档的触发器不能再运行。
         */
        post: operations["run_trigger_api_triggers__trigger_id__run_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/triggers/{trigger_id}/webhook/token": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /**
         * Generate Token
         * @description 生成（或重新生成）webhook 的 token；只能由用户在这里生成，旧 token 立即失效。
         */
        post: operations["generate_token_api_triggers__trigger_id__webhook_token_post"];
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
        /**
         * Upload
         * @description 请求体就是文件内容，Content-Type 是它的类型（与 Matrix /media/upload 相同，不用 multipart），边读边写，超限即停。
         */
        post: operations["upload_api_files_post"];
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
        /** Download */
        get: operations["download_api_files__file_id___name__get"];
        put?: never;
        post?: never;
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
        /** List Notifications */
        get: operations["list_notifications_api_notifications_get"];
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
        /** Unread */
        get: operations["unread_api_notifications_unread_get"];
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
        /** Mark Read */
        post: operations["mark_read_api_notifications_read_post"];
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
        /**
         * Mark Issue Read
         * @description 打开了 issue：请用户审核它的通知标为已读。
         */
        post: operations["mark_issue_read_api_notifications_issues__issue_id__read_post"];
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
         * Usage Summary
         * @description [since, until) 之间的用量，按给出的维度分组；不给维度就只有一行总计。
         *
         *     since / until 必须带时区（`2026-03-01T00:00:00Z`）：不带时区的时刻没有确定含义，在边界上拒绝。
         *     「天」按服务端配置的时区（`timezone`）划分日历日，与客户端所在时区无关。
         */
        get: operations["usage_summary_api_usage_get"];
        put?: never;
        post?: never;
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
        /** List Issues */
        get: operations["list_issues_api_issues_get"];
        put?: never;
        /** Create Issue */
        post: operations["create_issue_api_issues_post"];
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
        /** Get Issue */
        get: operations["get_issue_api_issues__issue_id__get"];
        put?: never;
        post?: never;
        /** Delete Issue */
        delete: operations["delete_issue_api_issues__issue_id__delete"];
        options?: never;
        head?: never;
        /** Update Issue */
        patch: operations["update_issue_api_issues__issue_id__patch"];
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
        /** Comment Issue */
        post: operations["comment_issue_api_issues__issue_id__comments_post"];
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
        /**
         * Approve Issue
         * @description 审核通过指派给用户的 issue：设为 done。没有指派给用户时 409。
         */
        post: operations["approve_issue_api_issues__issue_id__approve_post"];
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
         * Reject Issue
         * @description 打回指派给用户的 issue：理由记成评论，回到 todo，指派给把它交给用户的 agent。没有指派给用户、或不是 agent 交来的时 409。
         */
        post: operations["reject_issue_api_issues__issue_id__reject_post"];
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
        /**
         * Agent History
         * @description agent 的工作历史：没给 query 按日期倒序；给了按关键词检索（规则同工具 history_search）。since / until 含两端。
         */
        get: operations["agent_history_api_agents__agent_id__history_get"];
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
        /**
         * Features
         * @description 服务端配置决定的可选能力，界面据此显示或隐藏对应入口。
         */
        get: operations["features_api_features_get"];
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
        /**
         * Transcribe
         * @description 请求体是一段语音（webm、ogg、mp4、wav、mp3……），返回转写出的文字；转写服务出错时 502 并带上原因。
         */
        post: operations["transcribe_api_transcriptions_post"];
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
        /** Me */
        get: operations["me_api_auth_me_get"];
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
        /** Events */
        get: operations["events_api_events_get"];
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
        /**
         * ActivityKind
         * @enum {string}
         */
        ActivityKind: "created" | "comment" | "status" | "assignee" | "priority" | "owner" | "title" | "description" | "depends_on" | "dependency" | "run_failed";
        /** ActivityOut */
        ActivityOut: {
            /** Id */
            id: number;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Actor Agent Id */
            actor_agent_id: string | null;
            kind: components["schemas"]["ActivityKind"];
            /** Body */
            body: string | null;
            /** Change */
            change: {
                [key: string]: unknown;
            } | null;
        };
        /**
         * AgentAnswer
         * @description 被问的 agent 交回的结局（sessions.delegation）：给发问方模型的文字，它挂给用户的文件，所在的 thread。
         */
        AgentAnswer: {
            /** Text */
            text: string;
            /** Attachments */
            attachments: components["schemas"]["Attachment"][];
            /** Thread */
            thread: string;
        };
        /**
         * AgentAnswered
         * @description 回答信件里一次 ask_agent 调用的结果：被问的 agent 的结局。
         */
        AgentAnswered: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            tool: "ask_agent";
            result: components["schemas"]["AgentAnswer"];
        };
        /** AgentCreate */
        AgentCreate: {
            /** Name */
            name: string;
            /** Description */
            description: string;
            /** Model */
            model?: string | null;
            /** Roles */
            roles?: string[] | null;
            /**
             * Allow
             * @default []
             */
            allow: string[];
            /**
             * Deny
             * @default []
             */
            deny: string[];
            /**
             * Thinking
             * @default default
             * @enum {string}
             */
            thinking: "default" | "off" | "low" | "medium" | "high" | "xhigh";
        };
        /** AgentDefaultsOut */
        AgentDefaultsOut: {
            /** Model */
            model: string;
            /** Roles */
            roles: string[];
        };
        /** AgentOut */
        AgentOut: {
            /** Id */
            id: string;
            /** Name */
            name: string;
            /** Description */
            description: string;
            /** Model */
            model: string;
            /** Roles */
            roles: string[];
            /** Allow */
            allow: string[];
            /** Deny */
            deny: string[];
            /**
             * Thinking
             * @enum {string}
             */
            thinking: "default" | "off" | "low" | "medium" | "high" | "xhigh";
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Is Default */
            is_default: boolean;
        };
        /**
         * AgentUpdate
         * @description 整体替换：所有字段必填，不会用默认值覆盖已有配置。name 可改（只用于展示）。
         */
        AgentUpdate: {
            /** Name */
            name: string;
            /** Description */
            description: string;
            /** Model */
            model: string;
            /** Roles */
            roles: string[];
            /** Allow */
            allow: string[];
            /** Deny */
            deny: string[];
            /**
             * Thinking
             * @enum {string}
             */
            thinking: "default" | "off" | "low" | "medium" | "high" | "xhigh";
        };
        /** AnsweredQuestion */
        AnsweredQuestion: {
            /** Question */
            question: string;
            /** Selected */
            selected: string[];
            /** Other */
            other: string | null;
        };
        /**
         * AskResult
         * @description 一次提问交给模型的结果（也是界面展示已回答提问的来源）：answered 区分用户回答了，还是没回答就发了新消息。
         */
        AskResult: {
            /** Answered */
            answered: boolean;
            /** Answers */
            answers: components["schemas"]["AnsweredQuestion"][];
            /** Note */
            note: string | null;
        };
        /**
         * AskView
         * @description ask_user 调用的状态，由后端按唯一规则给出（annotate_asks），前端只渲染：
         *     asking 运行还没停下（还不能回答）；pending 现在能回答（显示表单）；submitted 回答已提交、还排着；answered 已回答；
         *     not_answered 用户没回答（发了新消息或只回答了别的提问）。
         */
        AskView: {
            /**
             * Status
             * @enum {string}
             */
            status: "asking" | "pending" | "submitted" | "answered" | "not_answered";
            /** Questions */
            questions: components["schemas"]["Question"][];
            /** Answers */
            answers: components["schemas"]["AnsweredQuestion"][];
            /** Note */
            note: string | null;
        };
        /** Attachment */
        Attachment: {
            /** Url */
            url: string;
            /** Name */
            name: string;
            /** Media Type */
            media_type: string;
            /** Inline */
            inline: boolean;
        };
        /** CommentIn */
        CommentIn: {
            /** Body */
            body: string;
        };
        /** CompactOut */
        CompactOut: {
            /** Queued */
            queued: boolean;
        };
        /** CompactionOut */
        CompactionOut: {
            /** Through Message Id */
            through_message_id: number;
            /** Summary */
            summary: string;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
        };
        /**
         * CorruptOut
         * @description 一个读不出来的资源文件（agents.api.CorruptResource）：界面列出来，供删除（skill 也可重写）。
         */
        CorruptOut: {
            /** Owner */
            owner: string | null;
            /** Name */
            name: string;
            /** Problem */
            problem: string;
        };
        /** EnabledIn */
        EnabledIn: {
            /** Enabled */
            enabled: boolean;
        };
        /** FeaturesOut */
        FeaturesOut: {
            /** Transcription */
            transcription: boolean;
        };
        /** FileOut */
        FileOut: {
            /** Id */
            id: string;
            /** Name */
            name: string;
            /** Media Type */
            media_type: string;
            /** Size */
            size: number;
            /** Url */
            url: string;
            /** Inline */
            inline: boolean;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
        };
        /** FiredOut */
        FiredOut: {
            /** Session Id */
            session_id: string | null;
        };
        /** HTTPValidationError */
        HTTPValidationError: {
            /** Detail */
            detail?: components["schemas"]["ValidationError"][];
        };
        /**
         * HeartbeatEvent
         * @description 连上就发一次、之后每隔一段时间发一次：前端据此判断连接是否还活着（间隔随心跳下发），并比较前端构建的版本。
         */
        HeartbeatEvent: {
            /**
             * Type
             * @default heartbeat
             * @constant
             */
            type: "heartbeat";
            /** Interval Seconds */
            interval_seconds: number;
            /** Ui Version */
            ui_version: string | null;
        };
        /** HistoryOut */
        HistoryOut: {
            /** Id */
            id: number;
            /** Agent Id */
            agent_id: string;
            /**
             * Day
             * Format: date
             */
            day: string;
            /** Title */
            title: string;
            /** Summary */
            summary: string;
            /** Refs */
            refs: string[];
            /** Importance */
            importance: number;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
        };
        /** IssueCreate */
        IssueCreate: {
            /** Title */
            title: string;
            /** Owner */
            owner: string;
            /**
             * Description
             * @default
             */
            description: string;
            /** @default backlog */
            status: components["schemas"]["IssueStatus"];
            /** @default none */
            priority: components["schemas"]["IssuePriority"];
            /** Assignee */
            assignee?: string | null;
            /**
             * Depends On
             * @default []
             */
            depends_on: number[];
        };
        /** IssueDetailOut */
        IssueDetailOut: {
            issue: components["schemas"]["IssueOut"];
            /** Activities */
            activities: components["schemas"]["ActivityOut"][];
        };
        /** IssueOut */
        IssueOut: {
            /** Id */
            id: number;
            /** Title */
            title: string;
            /** Description */
            description: string;
            status: components["schemas"]["IssueStatus"];
            priority: components["schemas"]["IssuePriority"];
            /** Owner */
            owner: string | null;
            /** Assignee */
            assignee: string | null;
            /** Creator Agent Id */
            creator_agent_id: string | null;
            /** Sessions */
            sessions: {
                [key: string]: string;
            };
            assignee_session: components["schemas"]["WorkState"] | null;
            /** Stalled */
            stalled: boolean;
            /** Depends On */
            depends_on: number[];
            /** Blocks */
            blocks: number[];
            /** Waiting On */
            waiting_on: number[];
            /** Reviewable */
            reviewable: boolean;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
        };
        /**
         * IssuePatch
         * @description 只修改给出的字段。assignee 给出 null 表示取消指派，不出现表示不变。
         */
        IssuePatch: {
            /** Owner */
            owner?: string | null;
            /** Title */
            title?: string | null;
            /** Description */
            description?: string | null;
            status?: components["schemas"]["IssueStatus"] | null;
            priority?: components["schemas"]["IssuePriority"] | null;
            /** Assignee */
            assignee?: string | null;
            /** Depends On */
            depends_on?: number[] | null;
        };
        /**
         * IssuePriority
         * @enum {string}
         */
        IssuePriority: "none" | "urgent" | "high" | "medium" | "low";
        /**
         * IssueStatus
         * @enum {string}
         */
        IssueStatus: "backlog" | "todo" | "in_progress" | "in_review" | "blocked" | "done" | "cancelled";
        /** Item */
        Item: {
            /**
             * Type
             * @enum {string}
             */
            type: "user" | "event" | "assistant" | "thinking" | "tool_call" | "tool_result" | "tool_error" | "stopped";
            /** Text */
            text: string;
            /** Tool Name */
            tool_name: string | null;
            /** Check In */
            check_in: boolean;
            /** Tool Call Id */
            tool_call_id: string | null;
            /** Message Id */
            message_id: number | null;
            /** Attachments */
            attachments: components["schemas"]["Attachment"][];
            /** Threads */
            threads: string[];
            /** Images */
            images: components["schemas"]["Attachment"][];
            ask: components["schemas"]["AskView"] | null;
            /** At */
            at: string | null;
        };
        /** LiveRunOut */
        LiveRunOut: {
            /** Run Id */
            run_id: string;
            /** Events */
            events: (components["schemas"]["RunItemEvent"] | components["schemas"]["RunDeltaEvent"])[];
        };
        /** LoginIn */
        LoginIn: {
            /** Password */
            password: string;
        };
        /**
         * MailKind
         * @description 信箱里的信件（sessions.mailbox）。
         * @enum {string}
         */
        MailKind: "message" | "answer" | "event";
        /**
         * MailOut
         * @description 会话信箱里的一封信件（sessions.mailbox）。
         */
        MailOut: {
            /** Id */
            id: number;
            kind: components["schemas"]["MailKind"];
            /** Source */
            source: string | null;
            /** Check In */
            check_in: boolean;
            /** Content */
            content: string;
            /** Attachments */
            attachments: components["schemas"]["FileOut"][];
            /** Answers */
            answers: {
                [key: string]: components["schemas"]["UserAnswered"] | components["schemas"]["AgentAnswered"];
            };
            state: components["schemas"]["MailState"];
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
        };
        /**
         * MailState
         * @enum {string}
         */
        MailState: "queued" | "consumed" | "discarded";
        /** MarkReadIn */
        MarkReadIn: {
            /** Through Id */
            through_id: number;
        };
        /** McpIn */
        McpIn: {
            /** Owner */
            owner: string | null;
            /** Name */
            name: string;
            transport: components["schemas"]["McpTransport"];
            /** Command */
            command?: string | null;
            /**
             * Args
             * @default []
             */
            args: string[];
            /** Url */
            url?: string | null;
            /**
             * Env
             * @default {}
             */
            env: {
                [key: string]: string;
            };
            /**
             * Headers
             * @default {}
             */
            headers: {
                [key: string]: string;
            };
            /**
             * Probe
             * @default true
             */
            probe: boolean;
        };
        /** McpOut */
        McpOut: {
            /** Owner */
            owner: string | null;
            /** Name */
            name: string;
            transport: components["schemas"]["McpTransport"];
            /** Command */
            command: string | null;
            /** Args */
            args: string[];
            /** Url */
            url: string | null;
            /** Env */
            env: {
                [key: string]: string;
            };
            /** Headers */
            headers: {
                [key: string]: string;
            };
            /** Enabled */
            enabled: boolean;
            /**
             * Tools
             * @default []
             */
            tools: string[];
            /** Shadowed */
            shadowed: boolean;
        };
        /**
         * McpTransport
         * @enum {string}
         */
        McpTransport: "stdio" | "http";
        /** MemoryIn */
        MemoryIn: {
            /** Owner */
            owner: string | null;
            /** Content */
            content: string;
        };
        /** MemoryOut */
        MemoryOut: {
            /** Content */
            content: string;
            /** Max Chars */
            max_chars: number;
        };
        /**
         * NotificationEvent
         * @description 写下了一条新通知（推给前端 SSE，前端据此刷新通知页与未读数）。与有没有推送订阅无关。
         */
        NotificationEvent: {
            /**
             * Type
             * @default notification
             * @constant
             */
            type: "notification";
            /** Notification Id */
            notification_id: number;
            /** Session Id */
            session_id: string | null;
        };
        /** NotificationOut */
        NotificationOut: {
            /** Id */
            id: number;
            /** Title */
            title: string;
            /** Body */
            body: string;
            /** Session Id */
            session_id: string | null;
            /** Issue Id */
            issue_id: number | null;
            /** Url */
            url: string | null;
            /** Unread */
            unread: boolean;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
        };
        /** OnceConfig */
        OnceConfig: {
            /**
             * At
             * Format: date-time
             */
            at: string;
        };
        /** OnceStatus */
        OnceStatus: {
            /**
             * Run At
             * Format: date-time
             */
            run_at: string;
        };
        /** OnceTriggerOut */
        OnceTriggerOut: {
            /** Id */
            id: number;
            /** Name */
            name: string;
            /** Agent Id */
            agent_id: string;
            /** Prompt */
            prompt: string;
            /** Enabled */
            enabled: boolean;
            precheck: components["schemas"]["Precheck"] | null;
            /** Last Checked At */
            last_checked_at: string | null;
            /** Last Fired At */
            last_fired_at: string | null;
            /** Last Session Id */
            last_session_id: string | null;
            /** Disabled Reason */
            disabled_reason: string | null;
            /** Precheck Error */
            precheck_error: string | null;
            /** Finished At */
            finished_at: string | null;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "once";
            config: components["schemas"]["OnceConfig"];
            status: components["schemas"]["OnceStatus"];
        };
        /** Option */
        Option: {
            /**
             * Label
             * @description Short choice text shown on the button.
             */
            label: string;
            /**
             * Description
             * @description What choosing it means, if not obvious.
             * @default
             */
            description: string;
        };
        /**
         * Origin
         * @description 会话来源：用户在网页发起、触发器（定时、外部事件……）、agent 之间委派、issue 看板唤醒被指派的 agent。
         * @enum {string}
         */
        Origin: "user" | "trigger" | "delegation" | "issue";
        /**
         * PermissionOut
         * @description 一个工具的权限（agents.permissions.TOOL_PERMISSIONS）。
         */
        PermissionOut: {
            /** Tool */
            tool: string;
            /** Id */
            id: string;
            /**
             * Access
             * @enum {string}
             */
            access: "read" | "write" | "irreversible";
            /** Note */
            note: string;
        };
        /** Precheck */
        Precheck: {
            /**
             * Command
             * @description bash command run before each firing.
             */
            command: string;
            /**
             * Secrets
             * @description Vault keys exposed to the command as env vars.
             * @default []
             */
            secrets: string[];
            /**
             * Fire When
             * @description 'output': fire when stdout is non-empty. 'changed': fire when stdout differs from the last run (the first run only records it).
             * @default output
             * @enum {string}
             */
            fire_when: "output" | "changed";
        };
        /** PushKeys */
        PushKeys: {
            /** P256Dh */
            p256dh: string;
            /** Auth */
            auth: string;
        };
        /** PushSubscriptionIn */
        PushSubscriptionIn: {
            /** Endpoint */
            endpoint: string;
            keys: components["schemas"]["PushKeys"];
        };
        /** Question */
        Question: {
            /**
             * Question
             * @description The full question.
             */
            question: string;
            /**
             * Header
             * @description Very short label, e.g. 'Timeframe'.
             */
            header: string;
            /** Options */
            options: components["schemas"]["Option"][];
            /**
             * Multi Select
             * @description Allow choosing several options.
             * @default false
             */
            multi_select: boolean;
        };
        /**
         * QuestionAnswer
         * @description 用户对一个问题的回答：选中的选项，和/或「其他」里填的文字。
         */
        QuestionAnswer: {
            /**
             * Selected
             * @default []
             */
            selected: string[];
            /** Other */
            other?: string | null;
        };
        /** RejectIn */
        RejectIn: {
            /** Reason */
            reason: string;
        };
        /** ReplyIn */
        ReplyIn: {
            /** Prompt */
            prompt: string;
            /**
             * Attachments
             * @default []
             */
            attachments: string[];
            /**
             * Answers
             * @default {}
             */
            answers: {
                [key: string]: components["schemas"]["QuestionAnswer"][];
            };
        };
        /**
         * ResolvedOut
         * @description 按规则最终能用的工具（不含 MCP），以及是否可能放行 MCP 工具；能开临时 agent 时另有临时 agent 按同一份规则能用的。
         */
        ResolvedOut: {
            /** Tools */
            tools: components["schemas"]["PermissionOut"][];
            /** Mcp */
            mcp: boolean;
            temp_agent: components["schemas"]["ResolvedOut"] | null;
        };
        /**
         * ResourceKind
         * @enum {string}
         */
        ResourceKind: "memory";
        /** RestoreIn */
        RestoreIn: {
            /** Owner */
            owner?: string | null;
            /** Rev */
            rev: string;
        };
        /** RevisionOut */
        RevisionOut: {
            /** Rev */
            rev: string;
            /** Content */
            content: string;
            /** Author */
            author: string;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
        };
        /** RoleIn */
        RoleIn: {
            /** Description */
            description: string;
            /** Allow */
            allow: string[];
            /** Deny */
            deny: string[];
        };
        /** RoleOut */
        RoleOut: {
            /** Name */
            name: string;
            /** Description */
            description: string;
            /** Allow */
            allow: string[];
            /** Deny */
            deny: string[];
            /** Builtin */
            builtin: boolean;
            resolved: components["schemas"]["ResolvedOut"];
        };
        /** RulesIn */
        RulesIn: {
            /** Roles */
            roles: string[];
            /** Allow */
            allow: string[];
            /** Deny */
            deny: string[];
        };
        /**
         * RunDeltaEvent
         * @description 流式文字：接在同类的最后一条后面（或开始新的一条）。
         */
        RunDeltaEvent: {
            /**
             * Type
             * @default run
             * @constant
             */
            type: "run";
            /** Session Id */
            session_id: string;
            /** Agent */
            agent: string;
            /** Run Id */
            run_id: string;
            /** Seq */
            seq: number;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            event: "delta";
            /**
             * Item Type
             * @enum {string}
             */
            item_type: "assistant" | "thinking";
            /** Text */
            text: string;
        };
        /**
         * RunItemEvent
         * @description 一条完整的会话条目（工具调用、工具结果），与历史同一个投影（transcript）；文字可能截断过。
         */
        RunItemEvent: {
            /**
             * Type
             * @default run
             * @constant
             */
            type: "run";
            /** Session Id */
            session_id: string;
            /** Agent */
            agent: string;
            /** Run Id */
            run_id: string;
            /** Seq */
            seq: number;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            event: "item";
            item: components["schemas"]["Item"];
        };
        /**
         * SavedEvent
         * @description 运行中又保存了一步（LiveRuns.step）：历史有了新内容，实时事件换成新的一段。
         */
        SavedEvent: {
            /**
             * Type
             * @default saved
             * @constant
             */
            type: "saved";
            /** Session Id */
            session_id: string;
        };
        /** ScheduleConfig */
        ScheduleConfig: {
            /** Cron */
            cron: string;
        };
        /** ScheduleStatus */
        ScheduleStatus: {
            /**
             * Next Run At
             * Format: date-time
             */
            next_run_at: string;
            skipped: components["schemas"]["SkippedFires"] | null;
        };
        /** ScheduleTriggerOut */
        ScheduleTriggerOut: {
            /** Id */
            id: number;
            /** Name */
            name: string;
            /** Agent Id */
            agent_id: string;
            /** Prompt */
            prompt: string;
            /** Enabled */
            enabled: boolean;
            precheck: components["schemas"]["Precheck"] | null;
            /** Last Checked At */
            last_checked_at: string | null;
            /** Last Fired At */
            last_fired_at: string | null;
            /** Last Session Id */
            last_session_id: string | null;
            /** Disabled Reason */
            disabled_reason: string | null;
            /** Precheck Error */
            precheck_error: string | null;
            /** Finished At */
            finished_at: string | null;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "schedule";
            config: components["schemas"]["ScheduleConfig"];
            status: components["schemas"]["ScheduleStatus"];
        };
        /** SecretIn */
        SecretIn: {
            /** Value */
            value: string;
            /**
             * Description
             * @default
             */
            description: string;
        };
        /** SecretOut */
        SecretOut: {
            /** Owner */
            owner: string | null;
            /** Key */
            key: string;
            /** Description */
            description: string;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
            /** Shadowed */
            shadowed: boolean;
        };
        /**
         * ServerEvent
         * @description SSE 里每条 data 的形状（api.app 的 /events）。具名是为了在 OpenAPI 里成为一个组件：前端的事件联合直接取它，
         *     不另列一遍（openapi-typescript 还不认识 OpenAPI 3.2 的 itemSchema）。OpenAPI 的 discriminator 不能嵌套，这里不标。
         */
        ServerEvent: components["schemas"]["TaskEvent"] | components["schemas"]["SavedEvent"] | components["schemas"]["RunItemEvent"] | components["schemas"]["RunDeltaEvent"] | components["schemas"]["NotificationEvent"] | components["schemas"]["HeartbeatEvent"];
        /** SessionDetail */
        SessionDetail: {
            session: components["schemas"]["SessionOut"];
            /** Items */
            items: components["schemas"]["Item"][];
            /** Compactions */
            compactions: components["schemas"]["CompactionOut"][];
            /** Mail */
            mail: components["schemas"]["MailOut"][];
            run: components["schemas"]["TaskOut"] | null;
            /** Threads */
            threads: components["schemas"]["SessionOut"][];
            live: components["schemas"]["LiveRunOut"] | null;
            state: components["schemas"]["WorkState"];
        };
        /**
         * SessionKind
         * @enum {string}
         */
        SessionKind: "conversation" | "thread" | "private_thread";
        /**
         * SessionListItem
         * @description 会话列表里的一行：多一个只在列表里算的工作状态（在运行 / 在等后台任务、委派或用户回答 / 空闲；会话页看
         *     SessionDetail.run）。
         */
        SessionListItem: {
            /** Id */
            id: string;
            /** Agent Id */
            agent_id: string;
            /** Title */
            title: string;
            origin: components["schemas"]["Origin"];
            /** Origin Ref */
            origin_ref: string | null;
            /** Parent Session Id */
            parent_session_id: string | null;
            /** Thread */
            thread: string | null;
            kind: components["schemas"]["SessionKind"];
            /** User Writable */
            user_writable: boolean;
            /** Deletable */
            deletable: boolean;
            /** Private */
            private: boolean;
            /** Unread */
            unread: boolean;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
            state: components["schemas"]["WorkState"];
        };
        /** SessionOut */
        SessionOut: {
            /** Id */
            id: string;
            /** Agent Id */
            agent_id: string;
            /** Title */
            title: string;
            origin: components["schemas"]["Origin"];
            /** Origin Ref */
            origin_ref: string | null;
            /** Parent Session Id */
            parent_session_id: string | null;
            /** Thread */
            thread: string | null;
            kind: components["schemas"]["SessionKind"];
            /** User Writable */
            user_writable: boolean;
            /** Deletable */
            deletable: boolean;
            /** Private */
            private: boolean;
            /** Unread */
            unread: boolean;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /**
             * Updated At
             * Format: date-time
             */
            updated_at: string;
        };
        /** SkillDetail */
        SkillDetail: {
            /** Name */
            name: string;
            /** Description */
            description: string;
            /** Owner */
            owner: string | null;
            /** Body */
            body: string;
        };
        /** SkillImportOut */
        SkillImportOut: {
            skill: components["schemas"]["SkillOut"];
            /** Skipped */
            skipped: components["schemas"]["SkippedOut"][];
        };
        /** SkillIn */
        SkillIn: {
            /** Owner */
            owner: string | null;
            /** Name */
            name: string;
            /** Description */
            description: string;
            /** Body */
            body: string;
        };
        /** SkillOut */
        SkillOut: {
            /** Name */
            name: string;
            /** Description */
            description: string;
            /** Owner */
            owner: string | null;
        };
        /** SkippedFires */
        SkippedFires: {
            /** Count */
            count: number;
            /**
             * Since
             * Format: date-time
             */
            since: string;
            previous_state: components["schemas"]["WorkState"];
        };
        /** SkippedOut */
        SkippedOut: {
            /** Path */
            path: string;
            /** Reason */
            reason: string;
        };
        /** StartIn */
        StartIn: {
            /** Agent Id */
            agent_id: string;
            /** Prompt */
            prompt: string;
            /**
             * Attachments
             * @default []
             */
            attachments: string[];
        };
        /** StreamConfig */
        StreamConfig: {
            /** Command */
            command: string;
            /**
             * Secrets
             * @default []
             */
            secrets: string[];
            /**
             * Debounce Seconds
             * @default 5
             */
            debounce_seconds: number;
        };
        /** StreamStatus */
        StreamStatus: {
            /** Running Since */
            running_since: string | null;
            /** Error */
            error: string | null;
            /** Failures */
            failures: number;
            /** Stderr Log */
            stderr_log: string;
        };
        /** StreamTriggerOut */
        StreamTriggerOut: {
            /** Id */
            id: number;
            /** Name */
            name: string;
            /** Agent Id */
            agent_id: string;
            /** Prompt */
            prompt: string;
            /** Enabled */
            enabled: boolean;
            precheck: components["schemas"]["Precheck"] | null;
            /** Last Checked At */
            last_checked_at: string | null;
            /** Last Fired At */
            last_fired_at: string | null;
            /** Last Session Id */
            last_session_id: string | null;
            /** Disabled Reason */
            disabled_reason: string | null;
            /** Precheck Error */
            precheck_error: string | null;
            /** Finished At */
            finished_at: string | null;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "stream";
            config: components["schemas"]["StreamConfig"];
            status: components["schemas"]["StreamStatus"];
        };
        /**
         * TaskEvent
         * @description 一个 task 的状态变化（前端据此从数据库重新拉取），由 worker 发出。
         */
        TaskEvent: {
            /**
             * Type
             * @default task
             * @constant
             */
            type: "task";
            /** Task Id */
            task_id: number;
            /** Kind */
            kind: string;
            status: components["schemas"]["TaskStatus"];
            /** Session Id */
            session_id: string | null;
            /** Agent */
            agent: string | null;
            /** Error */
            error: string | null;
        };
        /** TaskOut */
        TaskOut: {
            /** Id */
            id: number;
            /**
             * Kind
             * @enum {string}
             */
            kind: "event" | "run" | "compaction" | "deliver";
            /** Agent Id */
            agent_id: string | null;
            /** Session Id */
            session_id: string | null;
            status: components["schemas"]["TaskStatus"];
            /** Attempts */
            attempts: number;
            /** Error */
            error: string | null;
            /**
             * Not Before
             * Format: date-time
             */
            not_before: string;
            /**
             * Created At
             * Format: date-time
             */
            created_at: string;
            /** Finished At */
            finished_at: string | null;
        };
        /**
         * TaskStatus
         * @enum {string}
         */
        TaskStatus: "pending" | "running" | "done" | "failed";
        /** TokenOut */
        TokenOut: {
            /** Token */
            token: string;
        };
        /** TranscriptOut */
        TranscriptOut: {
            /** Text */
            text: string;
        };
        /** TriggerCreate */
        TriggerCreate: {
            /** Name */
            name: string;
            /** Agent Id */
            agent_id: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "schedule" | "once" | "webhook" | "stream";
            /** Prompt */
            prompt: string;
            /** Config */
            config: {
                [key: string]: unknown;
            };
            precheck?: components["schemas"]["Precheck"] | null;
        };
        /** TriggerKindOut */
        TriggerKindOut: {
            /**
             * Name
             * @enum {string}
             */
            name: "schedule" | "once" | "webhook" | "stream";
            /** Description */
            description: string;
        };
        /**
         * TriggerPatch
         * @description 只修改给出的字段。precheck 给出 null 表示去掉前置检查，不出现表示不变。
         */
        TriggerPatch: {
            /** Prompt */
            prompt?: string | null;
            /** Config */
            config?: {
                [key: string]: unknown;
            } | null;
            /** Enabled */
            enabled?: boolean | null;
            precheck?: components["schemas"]["Precheck"] | null;
        };
        /** UnreadOut */
        UnreadOut: {
            /** Count */
            count: number;
        };
        /**
         * UsageGroup
         * @description 汇总时的分组维度，可以任意组合；没有选中的维度在结果里为 None。
         * @enum {string}
         */
        UsageGroup: "agent" | "model" | "day";
        /**
         * UsageOut
         * @description 一个分组的汇总。没有参与分组的维度为 null。
         *
         *     input_tokens 是输入总数，**已包含** cache_read_tokens 与 cache_write_tokens（pydantic-ai 的口径），
         *     展示时是总数与其中两部分的关系，不要相加。
         */
        UsageOut: {
            /** Agent Id */
            agent_id: string | null;
            /** Model */
            model: string | null;
            /** Day */
            day: string | null;
            /** Requests */
            requests: number;
            /** Input Tokens */
            input_tokens: number;
            /** Output Tokens */
            output_tokens: number;
            /** Cache Read Tokens */
            cache_read_tokens: number;
            /** Cache Write Tokens */
            cache_write_tokens: number;
        };
        /**
         * UserAnswered
         * @description 回答信件里一次 ask_user 调用的结果：用户的回答。
         */
        UserAnswered: {
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            tool: "ask_user";
            result: components["schemas"]["AskResult"];
        };
        /** ValidationError */
        ValidationError: {
            /** Location */
            loc: (string | number)[];
            /** Message */
            msg: string;
            /** Error Type */
            type: string;
            /** Input */
            input?: unknown;
            /** Context */
            ctx?: Record<string, never>;
        };
        /** VapidOut */
        VapidOut: {
            /** Public Key */
            public_key: string;
        };
        /**
         * WebhookConfig
         * @description webhook 没有配置项。
         */
        WebhookConfig: Record<string, never>;
        /** WebhookStatus */
        WebhookStatus: {
            /** Url */
            url: string;
            /** Has Token */
            has_token: boolean;
        };
        /** WebhookTriggerOut */
        WebhookTriggerOut: {
            /** Id */
            id: number;
            /** Name */
            name: string;
            /** Agent Id */
            agent_id: string;
            /** Prompt */
            prompt: string;
            /** Enabled */
            enabled: boolean;
            precheck: components["schemas"]["Precheck"] | null;
            /** Last Checked At */
            last_checked_at: string | null;
            /** Last Fired At */
            last_fired_at: string | null;
            /** Last Session Id */
            last_session_id: string | null;
            /** Disabled Reason */
            disabled_reason: string | null;
            /** Precheck Error */
            precheck_error: string | null;
            /** Finished At */
            finished_at: string | null;
            /**
             * @description discriminator enum property added by openapi-typescript
             * @enum {string}
             */
            kind: "webhook";
            config: components["schemas"]["WebhookConfig"];
            status: components["schemas"]["WebhookStatus"];
        };
        /**
         * WorkState
         * @description 会话此刻在不在处理事情。running 与 waiting 都是「事情还没做完」：等待中的会话结果回来会自动接着做，
         *     所以除了展示，别的判断（issue 看板的「没人在处理」、触发器避免重叠）都只区分 idle 与否。
         * @enum {string}
         */
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
    login_api_auth_login_post: {
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
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    logout_api_auth_logout_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    hook_api_hooks__name__post: {
        parameters: {
            query?: {
                session_id?: string | null;
            };
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FiredOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    agent_defaults_api_agent_defaults_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentDefaultsOut"];
                };
            };
        };
    };
    list_permissions_api_permissions_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PermissionOut"][];
                };
            };
        };
    };
    resolve_permissions_api_permissions_resolve_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RulesIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ResolvedOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_roles_api_roles_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
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
    update_role_api_roles__name__put: {
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
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoleOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_role_api_roles__name__post: {
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
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RoleOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_role_api_roles__name__delete: {
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
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_providers_api_providers_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
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
    list_provider_models_api_providers__name__models_get: {
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
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": string[];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_agents_api_agents_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
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
    create_agent_api_agents_post: {
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
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_agent_api_agents__agent_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    update_agent_api_agents__agent_id__put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AgentUpdate"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_agent_api_agents__agent_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    set_default_agent_api_agents__agent_id__default_put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    unset_default_agent_api_agents__agent_id__default_delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_memory_api_memory_get: {
        parameters: {
            query?: {
                owner?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MemoryOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    put_memory_api_memory_put: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MemoryIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MemoryOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_skills_api_skills_get: {
        parameters: {
            query?: {
                owner?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SkillOut"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_corrupt_skills_api_skills_corrupt_get: {
        parameters: {
            query?: {
                owner?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CorruptOut"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_skill_api_skill_get: {
        parameters: {
            query: {
                name: string;
                owner?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SkillDetail"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    put_skill_api_skill_put: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SkillIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SkillOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_skill_api_skill_delete: {
        parameters: {
            query: {
                name: string;
                owner?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    export_skill_api_skill_package_get: {
        parameters: {
            query: {
                name: string;
                owner?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description <name>.skill */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/zip": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    import_skill_api_skill_package_post: {
        parameters: {
            query?: {
                owner?: string | null;
                replace?: boolean;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SkillImportOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_sessions_api_sessions_get: {
        parameters: {
            query?: {
                agent_id?: string | null;
                origin?: components["schemas"]["Origin"][] | null;
                /** @description 发起方在它那个 origin 里的身份，如触发器的 id */
                origin_ref?: string | null;
                limit?: number;
                offset?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionListItem"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    start_session_api_sessions_post: {
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
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_session_api_sessions__session_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionDetail"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_session_api_sessions__session_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    reply_api_sessions__session_id__messages_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ReplyIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MailOut"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    stop_api_sessions__session_id__stop_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    compact_api_sessions__session_id__compact_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CompactOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    mark_read_api_sessions__session_id__read_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_tasks_api_tasks_get: {
        parameters: {
            query?: {
                status?: components["schemas"]["TaskStatus"] | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TaskOut"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    retry_task_api_tasks__task_id__retry_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                task_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TaskOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_mcp_api_mcp_get: {
        parameters: {
            query?: {
                owner?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["McpOut"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    add_mcp_api_mcp_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["McpIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["McpOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_mcp_api_mcp_delete: {
        parameters: {
            query: {
                name: string;
                owner?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_corrupt_mcp_api_mcp_corrupt_get: {
        parameters: {
            query?: {
                owner?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CorruptOut"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    probe_mcp_api_mcp_tools_post: {
        parameters: {
            query: {
                name: string;
                owner?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": string[];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    set_mcp_enabled_api_mcp_enabled_put: {
        parameters: {
            query: {
                name: string;
                owner?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["EnabledIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["McpOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_secrets_api_vault_get: {
        parameters: {
            query?: {
                owner?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SecretOut"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    set_secret_api_vault__key__put: {
        parameters: {
            query?: {
                owner?: string | null;
            };
            header?: never;
            path: {
                key: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SecretIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_secret_api_vault__key__delete: {
        parameters: {
            query?: {
                owner?: string | null;
            };
            header?: never;
            path: {
                key: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    vapid_key_api_push_vapid_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
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
    subscribe_api_push_subscriptions_post: {
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
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    test_push_api_push_test_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    history_api_resources__kind__history_get: {
        parameters: {
            query?: {
                owner?: string | null;
            };
            header?: never;
            path: {
                kind: components["schemas"]["ResourceKind"];
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RevisionOut"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    restore_api_resources__kind__restore_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                kind: components["schemas"]["ResourceKind"];
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RestoreIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    trigger_kinds_api_trigger_kinds_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
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
    list_triggers_api_triggers_get: {
        parameters: {
            query?: {
                /** @description 列已归档的（跑完的一次性触发器）而不是还会触发的 */
                finished?: boolean;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": (components["schemas"]["ScheduleTriggerOut"] | components["schemas"]["OnceTriggerOut"] | components["schemas"]["WebhookTriggerOut"] | components["schemas"]["StreamTriggerOut"])[];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_trigger_api_triggers_post: {
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
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScheduleTriggerOut"] | components["schemas"]["OnceTriggerOut"] | components["schemas"]["WebhookTriggerOut"] | components["schemas"]["StreamTriggerOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_trigger_api_triggers__trigger_id__delete: {
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
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    update_trigger_api_triggers__trigger_id__patch: {
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
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScheduleTriggerOut"] | components["schemas"]["OnceTriggerOut"] | components["schemas"]["WebhookTriggerOut"] | components["schemas"]["StreamTriggerOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    run_trigger_api_triggers__trigger_id__run_post: {
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
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FiredOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    generate_token_api_triggers__trigger_id__webhook_token_post: {
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
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TokenOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    upload_api_files_post: {
        parameters: {
            query: {
                name: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FileOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    download_api_files__file_id___name__get: {
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
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_notifications_api_notifications_get: {
        parameters: {
            query?: {
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["NotificationOut"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    unread_api_notifications_unread_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
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
    mark_read_api_notifications_read_post: {
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
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    mark_issue_read_api_notifications_issues__issue_id__read_post: {
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
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    usage_summary_api_usage_get: {
        parameters: {
            query: {
                since: string;
                until: string;
                group_by?: components["schemas"]["UsageGroup"][] | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UsageOut"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_issues_api_issues_get: {
        parameters: {
            query?: {
                status?: components["schemas"]["IssueStatus"][] | null;
                assignee?: string | null;
                owner?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["IssueOut"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_issue_api_issues_post: {
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
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["IssueOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_issue_api_issues__issue_id__get: {
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
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["IssueDetailOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_issue_api_issues__issue_id__delete: {
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
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    update_issue_api_issues__issue_id__patch: {
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
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["IssueOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    comment_issue_api_issues__issue_id__comments_post: {
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
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ActivityOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    approve_issue_api_issues__issue_id__approve_post: {
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
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["IssueOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    reject_issue_api_issues__issue_id__reject_post: {
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
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["IssueOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    agent_history_api_agents__agent_id__history_get: {
        parameters: {
            query?: {
                query?: string | null;
                since?: string | null;
                until?: string | null;
                min_importance?: number;
                limit?: number;
            };
            header?: never;
            path: {
                agent_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HistoryOut"][];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    features_api_features_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
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
    transcribe_api_transcriptions_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TranscriptOut"];
                };
            };
        };
    };
    me_api_auth_me_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    events_api_events_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "text/event-stream": unknown;
                };
            };
        };
    };
}
