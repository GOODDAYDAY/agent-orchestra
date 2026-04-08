"""SQLite repository — all database CRUD operations.

REQ-012 v2 schema (version 5):
    - drops `events` and `pending_events` tables (orchestrator pane is now the
      authoritative inter-agent transcript)
    - drops `agents.topic_subscriptions` and `agents.auto_respond` (no event bus)
    - adds `groups.workflow_id` (REQ-012 v2 F-08)
    - adds `AgentRole.orchestrator` enum value (REQ-012 v2 F-07)
    - adds `meta.schema_version='5'` for the destructive-reset detection mechanism
    - role templates rewritten for orchestrator/marker protocol (REQ-012 v2 F-05)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import aiosqlite

from agent_management.backend.models import (
    _now,
    Agent,
    AgentRole,
    AgentStatus,
    Group,
    RoleTemplate,
    Session,
)
from agent_management.shared.config import DB_PATH, SCHEMA_VERSION

logger = logging.getLogger(__name__)


class SchemaIncompatibleError(RuntimeError):
    """Raised by Repository.init() when the on-disk schema version does not match.

    REQ-012 v2 chooses destructive reset over migration scripts because the
    project is a local single-user tool with no production data. The frontend
    catches this and shows a reset modal.
    """

    def __init__(self, actual: int, expected: int) -> None:
        super().__init__(
            f"SQLite schema version mismatch: on-disk={actual}, expected={expected}"
        )
        self.actual = actual
        self.expected = expected


class Repository:
    """Async SQLite repository.  One instance shared by all backend components."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Open connection, validate schema version, create schema if fresh.

        Raises SchemaIncompatibleError if an existing DB has a different schema
        version. Caller (frontend) must handle by prompting for destructive reset.
        """
        logger.info("Initialising database at %s", self._db_path)
        is_fresh = not self._db_path.exists()
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")

        if is_fresh:
            await self._create_schema()
            await self._set_schema_version(SCHEMA_VERSION)
            await self._seed_role_templates()
            await self._conn.commit()
            logger.info("Fresh database initialised at schema version %d", SCHEMA_VERSION)
            return

        # Existing DB — verify schema version before doing anything else.
        try:
            actual = await self._read_schema_version()
        except Exception:
            actual = 0
        if actual != SCHEMA_VERSION:
            await self._conn.close()
            self._conn = None
            raise SchemaIncompatibleError(actual=actual, expected=SCHEMA_VERSION)

        # Same version — make sure built-in templates are up to date in case
        # the bundled template content was edited without bumping schema.
        await self._seed_role_templates()
        await self._conn.commit()
        logger.info("Database initialised (existing, schema version %d)", SCHEMA_VERSION)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _create_schema(self) -> None:
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                id                  TEXT PRIMARY KEY,
                name                TEXT NOT NULL,
                role                TEXT NOT NULL,
                working_dir         TEXT NOT NULL,
                system_prompt       TEXT DEFAULT '',
                system_prompt_file  TEXT DEFAULT '',
                paused              INTEGER NOT NULL DEFAULT 0,
                status              TEXT NOT NULL DEFAULT 'not_started',
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS groups (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                workflow_id TEXT NOT NULL DEFAULT 'standard',
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS group_members (
                group_id    TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                agent_id    TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                PRIMARY KEY (group_id, agent_id)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id                  TEXT PRIMARY KEY,
                agent_id            TEXT NOT NULL REFERENCES agents(id),
                group_id            TEXT NOT NULL REFERENCES groups(id),
                claude_session_id   TEXT NOT NULL,
                previous_session_id TEXT DEFAULT '',
                tmux_session_name   TEXT DEFAULT '',
                tmux_pane_id        TEXT DEFAULT '',
                status              TEXT NOT NULL DEFAULT 'not_started',
                started_at          TEXT DEFAULT '',
                stopped_at          TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS role_templates (
                role            TEXT PRIMARY KEY,
                display_name    TEXT NOT NULL,
                system_prompt   TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS meta (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL
            );
        """)

    async def _read_schema_version(self) -> int:
        async with self._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ) as cur:
            row = await cur.fetchone()
        return int(row["value"]) if row else 0

    async def _set_schema_version(self, version: int) -> None:
        await self._conn.execute(
            """INSERT INTO meta (key, value) VALUES ('schema_version', ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(version),),
        )

    # ------------------------------------------------------------------
    # Role templates
    # ------------------------------------------------------------------

    # Bumped whenever the bundled template *content* changes. _seed_role_templates
    # detects mismatch via the `meta` table and force-overwrites all built-ins.
    # REQ-016 F-05: bumped to 6 because the orchestrator template gains a
    # new "技能调用规则" section and the worker templates gain a matching
    # "when asked to run /req-* invoke it" instruction.
    _TEMPLATE_VERSION = 6

    # Each tuple: (role, display_name, system_prompt)
    _DEFAULT_TEMPLATES: list[tuple[str, str, str]] = [
        # ------------------------ Orchestrator ------------------------
        ("orchestrator", "Orchestrator", """你是 Orchestrator —— 这个 group 的项目调度者。

## 你的工作流
{{WORKFLOW_DEFINITION}}

## 你的下属
{{WORKER_ROSTER}}

## 调度协议
- 当你想让某个下属做事时，输出一行 dispatch（推荐自闭合形式，不需要关闭标签）：

  <<DISPATCH role="developer" text="请实现 ...">>

  平台也接受带关闭标签的完整形式 <<DISPATCH ...>>...<</DISPATCH>>，两种都可以。
  role 必须是工作流中出现过的角色名（小写），text 是要发给下属的完整 prompt。
  text 内可以使用 \\" 转义双引号。text 内不要写换行符，整个 dispatch 写在一行。

- 平台会捕获这个 block，把 text 内容发送给对应下属的终端。
- 下属收到后会执行任务，并在最后一行输出完成标记 {{COMPLETION_MARKER}}。
- 你会看到平台返回一段:

  [WORKER_RESULT role="developer" via="marker"]
  ...下属的输出...
  [/WORKER_RESULT]

  这就是该下属的工作成果。via 标注完成是怎么检测到的:
  marker = 下属正常输出了 {{COMPLETION_MARKER}} 标记
  silence = 下属沉默超时（产物可能不完整）
  stall = 平台强制推进（产物可能严重不完整）

- 收到 [WORKER_RESULT] 后，你要决定下一步：
  · 如果工作流还没走完，继续 dispatch 下一个角色；
  · 如果当前是 Tester 且 via=marker 且产物含 <<TESTS_FAILED>>，按工作流定义回到上一个 Developer 步骤重新 dispatch（注意 max retries）；
  · 如果工作流的所有步骤都完成，输出 <<WORKFLOW_COMPLETE>> 然后停下；
  · 如果遇到无法继续的错误，输出 <<WORKFLOW_ABORT reason="..."/>>。

## 错误反馈
平台可能会用以下消息回复你（不是 [WORKER_RESULT]）：
- [PLATFORM_ERROR: ...] —— 你刚才的 dispatch 写错了（角色名错、text 含禁用标记等），改一下重发。
- [WORKER_ERROR role="X" reason="..."] —— 那个下属的 pane 不可用。考虑跳过或 abort。
- [PLATFORM_STALL: ...] —— 上一个 dispatch 卡住了，操作员被通知。等待操作员的处理结果（会以 [WORKER_RESULT] 形式回来）。

## 技能调用规则 (REQ-016 F-05)

工作流里的每一步都可能标注了一个必须调用的 /req-* 技能，例如：
  - /req-1-analyze  （需求分析）
  - /req-2-tech     （技术设计）
  - /req-3-code     （编码实现）
  - /req-4-security （安全审查）
  - /req-5-cleanup  （代码清理）
  - /req-6-review   （需求复审）
  - /req-7-verify   （构建/运行/测试验证）
  - /req-8-done     （归档）

查看上面的 "你的工作流" 部分，每一步 { { WORKFLOW_DEFINITION }} 的末尾如果标了
"⚡ must invoke /req-X"，那说明你 dispatch 给该角色时，必须在你的 dispatch text 里
明确命令该 worker 调用这个技能。

推荐 dispatch 格式：

  <<DISPATCH role="developer" text="请调用 /req-3-code 技能，目标是：<实现上一个 [WORKER_RESULT] 中的技术方案>。完成后请继续依次运行 /req-4-security、/req-5-cleanup、/req-6-review、/req-7-verify，全部通过后在最后一行输出 <<TASK_DONE>>">>

如果该步骤没有标技能（显示 "(no skill — human review step)"），正常下达指令即可，
不要瞎编技能名。只能引用工作流里真正出现过的 /req-* 技能名。

## 硬性规则
- 一次只能 dispatch 一个角色，必须等到 [WORKER_RESULT] 才能 dispatch 下一个。
- text 字段不能包含字符串 {{COMPLETION_MARKER}}、<<WORKFLOW_COMPLETE>> 或 <<WORKFLOW_ABORT —— 这些是平台控制标记。
- text 字段不要包含换行符；整个 dispatch 写在一行内。
- 工作流完成后只输出 <<WORKFLOW_COMPLETE>>，不要再 dispatch。
- 不要伪造 [WORKER_RESULT]，那只能由平台注入。
- 不要解释你的内部思考；直接产出 dispatch block 或 workflow 控制标记。
"""),

        # ------------------------ PM ------------------------
        ("product_manager", "Product Manager", """你是这个 group 的产品经理 (PM)。

## 你的职责
当 Orchestrator 把一个需求任务交给你时:
1. 把粗略的需求描述扩展为一份完整的需求草稿（背景、目标用户、功能点、验收标准、out of scope）。
2. 用尽可能具体、可验证的语言描述每一个功能点。
3. 不要写代码，不要写技术方案。

## 协议（必须遵守）
1. 你的所有任务都来自 Orchestrator —— 它会把任务 prompt 直接发到你的终端，你不需要去拉任何队列。
2. 完成任务后，必须在最后一行（且仅最后一行）输出：

   <<TASK_DONE>>

3. 永远不要把 <<TASK_DONE>> 写在中间任何位置，它只能作为整段输出的结束标记。
4. 完成 <<TASK_DONE>> 后停止；不要等"下一条消息"，不要调用任何工具去拉取队列。

## 输出格式
   <你的需求草稿正文，可以多段、可以任意长度>
   <<TASK_DONE>>

## 技能调用规则 (REQ-016 F-05)
如果 Orchestrator 发给你的 prompt 里提到了某个 /req-* 技能（比如 /req-1-analyze），
你必须在自己的终端里调用这个技能（就像用户手动打 /req-1-analyze 一样）。
调用完毕、产出需求文档后，再在最后一行输出 <<TASK_DONE>>。
不要跳过技能调用，不要假装执行过。
"""),

        # ------------------------ Tech Director ------------------------
        ("tech_director", "Tech Director", """你是这个 group 的技术总监 (Tech Director)。

## 你的职责
当 Orchestrator 把一份需求或一段代码交给你时:
1. 如果是需求：产出技术设计（架构、模块拆分、接口、数据模型、关键流程、风险）。
2. 如果是研究问题：产出调研发现和建议方案。
3. 不要直接写实现代码，只给 Developer 留下足够清晰的指令。

## 协议（必须遵守）
1. 你的所有任务都来自 Orchestrator —— 它会把任务 prompt 直接发到你的终端，你不需要去拉任何队列。
2. 完成任务后，必须在最后一行（且仅最后一行）输出：

   <<TASK_DONE>>

3. 永远不要把 <<TASK_DONE>> 写在中间任何位置。
4. 完成 <<TASK_DONE>> 后停止。

## 输出格式
   <你的技术设计或研究产出正文>
   <<TASK_DONE>>

## 技能调用规则 (REQ-016 F-05)
如果 Orchestrator 发给你的 prompt 里提到了某个 /req-* 技能（例如 /req-2-tech），
你必须在自己的终端里调用这个技能来产出技术设计文档。调用完毕之后再在最后一行
输出 <<TASK_DONE>>。不要跳过技能调用。
"""),

        # ------------------------ Developer ------------------------
        ("developer", "Developer", """你是这个 group 的开发工程师 (Developer)。

## 你的职责
当 Orchestrator 把一份技术设计（或一份测试失败报告）交给你时:
1. 严格按设计实现代码（或修复测试报告中的 bug）。
2. 修改代码、运行命令、写测试都允许；这是你的职责范围。
3. 完成后写一段简短总结，说明改了什么、为什么。

## 协议（必须遵守）
1. 你的所有任务都来自 Orchestrator —— 它会把任务 prompt 直接发到你的终端，你不需要去拉任何队列。
2. 完成任务后，必须在最后一行（且仅最后一行）输出：

   <<TASK_DONE>>

3. 永远不要把 <<TASK_DONE>> 写在中间任何位置 —— 即便你在记录命令输出或测试日志时碰巧写到这个字符串，也要改写或删除。
4. 完成 <<TASK_DONE>> 后停止；不要等"下一条消息"。

## 输出格式
   <实现总结：改了哪些文件、关键决策、遗留问题>
   <<TASK_DONE>>

## 技能调用规则 (REQ-016 F-05) — 关键，不能省略
如果 Orchestrator 发给你的 prompt 里提到 /req-3-code，你必须按顺序依次调用：

  1. /req-3-code      — 编码
  2. /req-4-security  — 安全审查
  3. /req-5-cleanup   — 代码清理
  4. /req-6-review    — 需求复审
  5. /req-7-verify    — 构建/运行/测试验证

每一步都必须真实执行，不要跳步，不要伪造结果。全部通过后在最后一行输出
<<TASK_DONE>>；任何一步失败都立即停止并在 <<TASK_DONE>> 之前写清楚失败原因。
"""),

        # ------------------------ Tester ------------------------
        ("tester", "Tester", """你是这个 group 的测试工程师 (Tester)。

## 你的职责
当 Orchestrator 把一段实现交给你时:
1. 设计并执行测试（单元、集成、烟雾、边界）。
2. 报告每条测试的：测试目的 / 步骤 / 期望 / 实际 / 结论。
3. 如果有任何测试失败，必须在 <<TASK_DONE>> 之前的某一行单独输出 <<TESTS_FAILED>> —— 这是给 Orchestrator 的信号，让它把工作交还给 Developer。
4. 不要修改业务代码（这是 Developer 的职责）；只能修改/创建测试文件。

## 协议（必须遵守）
1. 你的所有任务都来自 Orchestrator —— 它会把任务 prompt 直接发到你的终端，你不需要去拉任何队列。
2. 完成任务后，必须在最后一行（且仅最后一行）输出：

   <<TASK_DONE>>

3. 如果测试有失败：在 <<TASK_DONE>> 之前的一行单独输出 <<TESTS_FAILED>>。
4. 完成 <<TASK_DONE>> 后停止。

## 输出格式（全部通过的情况）
   <测试报告：每条测试的目的/步骤/期望/实际/结论>
   <<TASK_DONE>>

## 输出格式（有失败的情况）
   <测试报告 + 失败的复现步骤 + 期望与实际差异>
   <<TESTS_FAILED>>
   <<TASK_DONE>>

## 技能调用规则 (REQ-016 F-05)
如果 Orchestrator 发给你的 prompt 里提到 /req-7-verify，你必须在自己的终端里
调用这个技能来执行完整的测试验证流程，然后基于它的输出写你的测试报告。不要
跳过技能调用，不要凭空捏造测试结果。
"""),

        # ------------------------ User ------------------------
        ("user", "User", """你代表这个 group 的最终用户 (User)。

## 你的职责
当 Orchestrator 把一份"已完成"的工作产物交给你时:
1. 站在最终用户视角验收：可用性、是否解决了原始需求、是否有遗漏。
2. 产出一份验收结论：通过 / 需要小修 / 需要返工。
3. 如果有真人通过 tmux attach 介入，把控制权交给真人 —— 等待真人的输入再继续。

## 协议（必须遵守）
1. 你的所有任务都来自 Orchestrator —— 它会把任务 prompt 直接发到你的终端，你不需要去拉任何队列。
2. 完成任务后，必须在最后一行（且仅最后一行）输出：

   <<TASK_DONE>>

3. 永远不要把 <<TASK_DONE>> 写在中间任何位置 —— 但是如果有真人在 attach 模式下接手，真人可以手动输入 <<TASK_DONE>> 来推进工作流。
4. 完成 <<TASK_DONE>> 后停止。

## 输出格式
   <验收结论：通过/需要小修/需要返工 + 具体反馈>
   <<TASK_DONE>>
"""),

        ("custom", "Custom", ""),
    ]

    async def _seed_role_templates(self) -> None:
        """Insert or force-update built-in role templates.

        Strategy: detect template_version mismatch and force-overwrite all built-ins.
        Custom user edits to built-in templates are overwritten on bump (known
        limitation, still Out of Scope per requirement.md §5).
        """
        version_key = "template_version"
        async with self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (version_key,)
        ) as cur:
            row = await cur.fetchone()
        applied_version = int(row["value"]) if row else 0
        force_update = applied_version != self._TEMPLATE_VERSION

        for role, display_name, prompt in self._DEFAULT_TEMPLATES:
            if force_update:
                await self._conn.execute(
                    """INSERT INTO role_templates (role, display_name, system_prompt)
                       VALUES (?, ?, ?)
                       ON CONFLICT(role) DO UPDATE SET
                           display_name=excluded.display_name,
                           system_prompt=excluded.system_prompt""",
                    (role, display_name, prompt),
                )
            else:
                await self._conn.execute(
                    """INSERT OR IGNORE INTO role_templates
                       (role, display_name, system_prompt) VALUES (?, ?, ?)""",
                    (role, display_name, prompt),
                )

        await self._conn.execute(
            """INSERT INTO meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (version_key, str(self._TEMPLATE_VERSION)),
        )
        await self._conn.commit()

    async def reset_role_templates(self) -> None:
        """Overwrite all role templates with the current built-in defaults."""
        for role, display_name, prompt in self._DEFAULT_TEMPLATES:
            await self._conn.execute(
                """INSERT INTO role_templates (role, display_name, system_prompt)
                   VALUES (?, ?, ?)
                   ON CONFLICT(role) DO UPDATE SET
                       display_name=excluded.display_name,
                       system_prompt=excluded.system_prompt""",
                (role, display_name, prompt),
            )
        await self._conn.commit()

    async def get_role_templates(self) -> list[RoleTemplate]:
        async with self._conn.execute(
            "SELECT role, display_name, system_prompt FROM role_templates ORDER BY role"
        ) as cur:
            return [
                RoleTemplate(
                    role=AgentRole(r["role"]),
                    display_name=r["display_name"],
                    system_prompt=r["system_prompt"],
                )
                async for r in cur
            ]

    async def save_role_template(self, template: RoleTemplate) -> None:
        await self._conn.execute(
            """INSERT INTO role_templates (role, display_name, system_prompt)
               VALUES (?, ?, ?)
               ON CONFLICT(role) DO UPDATE SET
                   display_name=excluded.display_name,
                   system_prompt=excluded.system_prompt""",
            (template.role.value, template.display_name, template.system_prompt),
        )
        await self._conn.commit()

    async def get_orchestrator_template(self) -> str:
        """Return the orchestrator's system prompt template (with placeholders)."""
        async with self._conn.execute(
            "SELECT system_prompt FROM role_templates WHERE role='orchestrator'"
        ) as cur:
            row = await cur.fetchone()
        return row["system_prompt"] if row else ""

    # ------------------------------------------------------------------
    # Agent CRUD
    # ------------------------------------------------------------------

    async def save_agent(self, agent: Agent) -> None:
        agent.updated_at = _now()
        await self._conn.execute(
            """
            INSERT INTO agents
                (id, name, role, working_dir, system_prompt, system_prompt_file,
                 paused, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, role=excluded.role,
                working_dir=excluded.working_dir,
                system_prompt=excluded.system_prompt,
                system_prompt_file=excluded.system_prompt_file,
                paused=excluded.paused,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                agent.id, agent.name, agent.role.value, agent.working_dir,
                agent.system_prompt, agent.system_prompt_file,
                1 if agent.paused else 0,
                agent.status.value,
                agent.created_at, agent.updated_at,
            ),
        )
        await self._conn.commit()

    async def get_agents(self) -> list[Agent]:
        async with self._conn.execute("SELECT * FROM agents ORDER BY created_at") as cur:
            return [self._row_to_agent(row) async for row in cur]

    async def get_agent(self, agent_id: str) -> Optional[Agent]:
        async with self._conn.execute(
            "SELECT * FROM agents WHERE id=?", (agent_id,)
        ) as cur:
            row = await cur.fetchone()
            return self._row_to_agent(row) if row else None

    async def update_agent_status(self, agent_id: str, status: AgentStatus) -> None:
        await self._conn.execute(
            "UPDATE agents SET status=?, updated_at=? WHERE id=?",
            (status.value, _now(), agent_id),
        )
        await self._conn.commit()

    async def set_agent_paused(self, agent_id: str, paused: bool) -> None:
        await self._conn.execute(
            "UPDATE agents SET paused=?, updated_at=? WHERE id=?",
            (1 if paused else 0, _now(), agent_id),
        )
        await self._conn.commit()

    async def delete_agent(self, agent_id: str) -> None:
        await self._conn.execute("DELETE FROM sessions WHERE agent_id=?", (agent_id,))
        await self._conn.execute("DELETE FROM group_members WHERE agent_id=?", (agent_id,))
        await self._conn.execute("DELETE FROM agents WHERE id=?", (agent_id,))
        await self._conn.commit()

    async def clear_all_runtime_state(self) -> None:
        """Clear all sessions; reset agent statuses."""
        await self._conn.execute("DELETE FROM sessions")
        await self._conn.execute(
            "UPDATE agents SET status=?", (AgentStatus.not_started.value,)
        )
        await self._conn.commit()

    @staticmethod
    def _row_to_agent(row: aiosqlite.Row) -> Agent:
        return Agent(
            id=row["id"],
            name=row["name"],
            role=AgentRole(row["role"]),
            working_dir=row["working_dir"],
            system_prompt=row["system_prompt"] or "",
            system_prompt_file=row["system_prompt_file"] or "",
            paused=bool(row["paused"]),
            status=AgentStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # Group CRUD
    # ------------------------------------------------------------------

    async def save_group(self, group: Group) -> None:
        await self._conn.execute(
            """INSERT INTO groups (id, name, workflow_id, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, workflow_id=excluded.workflow_id""",
            (group.id, group.name, group.workflow_id, group.created_at),
        )
        await self._conn.commit()

    async def get_groups(self) -> list[Group]:
        async with self._conn.execute("SELECT * FROM groups ORDER BY created_at") as cur:
            return [
                Group(
                    id=r["id"],
                    name=r["name"],
                    workflow_id=r["workflow_id"] or "standard",
                    created_at=r["created_at"],
                )
                async for r in cur
            ]

    async def get_group(self, group_id: str) -> Optional[Group]:
        async with self._conn.execute(
            "SELECT * FROM groups WHERE id=?", (group_id,)
        ) as cur:
            row = await cur.fetchone()
            return Group(
                id=row["id"],
                name=row["name"],
                workflow_id=row["workflow_id"] or "standard",
                created_at=row["created_at"],
            ) if row else None

    async def set_workflow_id(self, group_id: str, workflow_id: str) -> None:
        await self._conn.execute(
            "UPDATE groups SET workflow_id=? WHERE id=?",
            (workflow_id, group_id),
        )
        await self._conn.commit()

    async def delete_group(self, group_id: str) -> None:
        await self._conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Group members
    # ------------------------------------------------------------------

    async def add_group_member(self, group_id: str, agent_id: str) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO group_members (group_id, agent_id) VALUES (?, ?)",
            (group_id, agent_id),
        )
        await self._conn.commit()

    async def remove_group_member(self, group_id: str, agent_id: str) -> None:
        await self._conn.execute(
            "DELETE FROM group_members WHERE group_id=? AND agent_id=?",
            (group_id, agent_id),
        )
        await self._conn.commit()

    async def get_group_member_ids(self, group_id: str) -> list[str]:
        async with self._conn.execute(
            "SELECT agent_id FROM group_members WHERE group_id=?", (group_id,)
        ) as cur:
            return [r["agent_id"] async for r in cur]

    async def get_group_members(self, group_id: str) -> list[Agent]:
        ids = await self.get_group_member_ids(group_id)
        agents = []
        for aid in ids:
            agent = await self.get_agent(aid)
            if agent:
                agents.append(agent)
        return agents

    async def get_orchestrator_for_group(self, group_id: str) -> Optional[Agent]:
        """REQ-012 v2 — return the AgentRole.orchestrator member of a group, if any."""
        members = await self.get_group_members(group_id)
        return next((a for a in members if a.role == AgentRole.orchestrator), None)

    async def get_workers_for_group(self, group_id: str) -> list[Agent]:
        """REQ-012 v2 — return all non-orchestrator members of a group."""
        members = await self.get_group_members(group_id)
        return [a for a in members if a.role != AgentRole.orchestrator]

    async def get_agent_groups(self, agent_id: str) -> list[Group]:
        async with self._conn.execute(
            """SELECT g.* FROM groups g
               JOIN group_members gm ON g.id = gm.group_id
               WHERE gm.agent_id=?""",
            (agent_id,),
        ) as cur:
            return [
                Group(
                    id=r["id"],
                    name=r["name"],
                    workflow_id=r["workflow_id"] or "standard",
                    created_at=r["created_at"],
                )
                async for r in cur
            ]

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def save_session(self, session: Session) -> None:
        await self._conn.execute(
            """
            INSERT INTO sessions
                (id, agent_id, group_id, claude_session_id, previous_session_id,
                 tmux_session_name, tmux_pane_id, status, started_at, stopped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                claude_session_id=excluded.claude_session_id,
                previous_session_id=excluded.previous_session_id,
                tmux_session_name=excluded.tmux_session_name,
                tmux_pane_id=excluded.tmux_pane_id,
                status=excluded.status,
                started_at=excluded.started_at,
                stopped_at=excluded.stopped_at
            """,
            (
                session.id, session.agent_id, session.group_id,
                session.claude_session_id, session.previous_session_id,
                session.tmux_session_name, session.tmux_pane_id,
                session.status.value, session.started_at, session.stopped_at,
            ),
        )
        await self._conn.commit()

    async def get_session(self, agent_id: str, group_id: str) -> Optional[Session]:
        async with self._conn.execute(
            """SELECT * FROM sessions WHERE agent_id=? AND group_id=?
               ORDER BY started_at DESC LIMIT 1""",
            (agent_id, group_id),
        ) as cur:
            row = await cur.fetchone()
            return self._row_to_session(row) if row else None

    async def get_sessions_for_group(self, group_id: str) -> list[Session]:
        async with self._conn.execute(
            "SELECT * FROM sessions WHERE group_id=? AND status NOT IN ('stopped','degraded')",
            (group_id,),
        ) as cur:
            return [self._row_to_session(r) async for r in cur]

    async def update_session_status(self, session_id: str, status: AgentStatus,
                                     stopped_at: str = "") -> None:
        await self._conn.execute(
            "UPDATE sessions SET status=?, stopped_at=? WHERE id=?",
            (status.value, stopped_at, session_id),
        )
        await self._conn.commit()

    async def update_session_pane(self, session_id: str, tmux_session_name: str,
                                   tmux_pane_id: str) -> None:
        await self._conn.execute(
            "UPDATE sessions SET tmux_session_name=?, tmux_pane_id=? WHERE id=?",
            (tmux_session_name, tmux_pane_id, session_id),
        )
        await self._conn.commit()

    @staticmethod
    def _row_to_session(row: aiosqlite.Row) -> Session:
        return Session(
            id=row["id"],
            agent_id=row["agent_id"],
            group_id=row["group_id"],
            claude_session_id=row["claude_session_id"],
            previous_session_id=row["previous_session_id"] or "",
            tmux_session_name=row["tmux_session_name"] or "",
            tmux_pane_id=row["tmux_pane_id"] or "",
            status=AgentStatus(row["status"]),
            started_at=row["started_at"] or "",
            stopped_at=row["stopped_at"] or "",
        )
