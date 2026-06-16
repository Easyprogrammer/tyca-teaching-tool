# Errors

Command failures and integration errors.

---

## [ERR-20260616-001] server_start_missing_app_secret

**Logged**: 2026-06-16T03:28:27Z
**Priority**: medium
**Status**: resolved
**Area**: config

### Summary
Starting the backend without `.env` or `APP_SECRET` fails fast.

### Error
```text
RuntimeError: APP_SECRET must be at least 16 characters
```

### Context
- Command attempted: `python3 server/app.py`
- The MVP intentionally validates `APP_SECRET` at startup.
- Local verification used a temporary environment variable instead of writing a real secret.

### Suggested Fix
Create `server/.env` from `server/.env.example` before manual startup, or pass a temporary `APP_SECRET` in the shell for local testing.

### Metadata
- Reproducible: yes
- Related Files: server/app.py, server/.env.example

---
## [ERR-20260616-001] smoke_test_assertion

**Logged**: 2026-06-16T12:08:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
新增阅读程序兜底解析 smoke test 时，把有 warning 但无 errors 的 run 状态误判为 `adapter_warning`。

### Error
```text
AssertionError at assert application_run["status"] == "adapter_warning"
```

### Context
- 后端状态规则是 `adapter_ready` 表示校验无阻断错误，即使存在 warnings。
- 阅读程序无 fenced code 的兜底解析会生成 warning，但不阻断预览和 adapter 生成。

### Suggested Fix
测试应断言 `adapter_ready` 且 `adapterValidation.warnings` 非空，覆盖“不再 400，但提示老师检查”的真实行为。

### Metadata
- Reproducible: yes
- Related Files: server/smoke_test.py, server/app.py

### Resolution
- **Resolved**: 2026-06-16T12:08:00+08:00
- **Notes**: 已修正 smoke test 断言。

---
## [ERR-20260616-002] choice_generation_hard_fail

**Logged**: 2026-06-16T12:12:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: backend

### Summary
选择题缺答案时 `/api/runs` 仍直接返回 400，导致老师只能看到单个错误，不能进入审查表批量修正。

### Error
```text
生成失败：选择题第 1 题缺少答案
```

### Context
- 页面反馈已就近显示，但后端生成流程仍把选择题缺答案作为请求级异常。
- 录题助手的目标是先生成 adapter 和审查表，缺答案应成为 adapterValidation errors，上传前再阻断。

### Suggested Fix
选择题生成时允许空答案，写入 `generationIssues`；校验阶段把缺正确答案和 generationIssues 转成 errors。

### Metadata
- Reproducible: yes
- Related Files: server/app.py, server/smoke_test.py

### Resolution
- **Resolved**: 2026-06-16T12:12:00+08:00
- **Notes**: 已添加缺答案选择题 smoke test。

---
## [ERR-20260616-003] remote_env_secret_not_passed

**Logged**: 2026-06-16T12:33:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary
配置服务器 DeepSeek 环境变量时，本地 shell 变量未传入远端 Python，导致远端读取 `DEEPSEEK_KEY` 失败。

### Error
```text
KeyError: 'DEEPSEEK_KEY'
```

### Context
- 远端服务仍保持 active，健康检查正常。
- 修正方式必须避免在输出、代码或仓库中暴露 API key。

### Suggested Fix
通过 stdin 或远端临时受控脚本写入密钥，执行后只回显健康检查，不打印环境文件内容。

### Metadata
- Reproducible: yes
- Related Files: deploy/server.env.example

### Resolution
- **Resolved**: 2026-06-16T12:33:00+08:00
- **Notes**: 改用非回显方式写入服务器环境文件。

---
## [ERR-20260616-004] fallback_knowledge_regex

**Logged**: 2026-06-16T12:46:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: backend

### Summary
新增知识点兜底规则时，多条 raw string 把 `\(`、`\[`、`\s` 写成了双反斜杠，导致正则编译失败，`/api/runs` 返回 500。

### Error
```text
re.error: missing ), unterminated subpattern
```

### Context
- 触发点是 `infer_knowledge_for_payload` 遍历 `KNOWLEDGE_FALLBACK_RULES`。
- raw string 中匹配字面量左括号应使用 `r"O\("`，匹配 `\s` 应使用 `r"\s"`。
- 后续还发现裸 `&` 会误命中 HTML 实体 `&lt;` / `&gt;`，导致普通循环题被误打成位运算。

### Suggested Fix
修正正则并通过 smoke test 覆盖 `/api/runs` 创建流程。

### Metadata
- Reproducible: yes
- Related Files: server/app.py, server/smoke_test.py

### Resolution
- **Resolved**: 2026-06-16T12:46:00+08:00
- **Notes**: 已修正所有兜底知识点规则，移除裸 `&` 位运算匹配，并增加 smoke test 编译检查。

---
