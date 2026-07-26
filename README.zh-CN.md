[English](README.md) | [简体中文](README.zh-CN.md)

# Organize — Codex + Notion 日常工作追踪器

> 一个 approval-first 的 Codex + Notion starter kit，用于追踪日常工作，并把已完成任务转化为可复用记录。

Organize 帮助你采用、追踪、复盘并维护一套轻量的 Notion 日常工作系统。
它让进行中的工作保持简单，同时补上有意识的完成后闭环，避免任务结束后有用经验随之消失。
所有受支持的 Notion 写入和清理步骤都保持可见，并以明确批准为前提。

[**使用当前可用的现有列表路径 →**](#现有-notion-列表路径)

> **产品状态：**现有列表采用路径已经可用：只读检查、经批准且被 Git 忽略的
> 私有 profile，以及同一 profile 的 Total/Organize readiness 均已实现。
> New System provisioning 规划在 v0.3，当前不是可执行路径。

<a id="choose-your-starting-path"></a>

## 选择你的起点

### 使用现有 Notion 列表 — 当前可用

从你已经在使用的工作系统开始。Organize 先以只读方式检查，报告哪些部分
`ready`、`missing` 或 `incompatible`，映射受支持的字段和归档目标，并提出
Total 与 Organize 所需的私有配置。

[查看现有列表路径](#现有-notion-列表路径)

### 建立新的 Notion 系统 — 规划于 v0.3

这条未来路径将帮助尚无兼容 workspace 的用户创建 source list、category
archives、ordered view、date anchors、block field 和私有配置。当前版本不会
创建或调整这些 workspace 结构。

## 适合谁

如果你希望做到以下事情，可以使用 Organize：

- 用轻量 Notion 列表记录日常工作，而不是维护复杂的项目管理系统；
- 在决定保留或清理什么之前复盘已完成工作；
- 把选中的 Takeaway 和 Improvement 变成按 Category 整理的可复用记录；
- 让 Notion 写入和清理始终处于明确控制之下。

## 不适合谁

Organize 不是独立任务管理器、单独 GUI、自动化平台或无人值守的 workspace
管理器。它不适用于静默创建数据库、一键迁移、广泛修复既有数据，或未经复盘
和明确确认就清理源记录的流程。

## 核心工作流

`Adopt → Track → total YYYY-MM → organize todo → Preserve → done → confirm`

1. **Adopt：**检查现有兼容系统，单独批准其本地私有 profile，并验证同一
   profile 的 readiness。
2. **Track：**在 Notion 中记录日常工作、完成状态、Category、block text，以及
   可选的 Takeaway 或 Improvement。
3. **查看月份统计：**如果需要，在 Organize 前运行 `total YYYY-MM`，只读查看
   已完成 blocks 的 Category 小计。这一步是推荐项而非强制依赖，也不会保存
   历史报表。
4. **复盘已完成工作：**运行 `organize todo`，再对每项选择 `ok`、`dismiss`
   或 `skip`。数字 `undo` 只适用于它受支持的当前 active batch 范围。
5. **Preserve：**`Category` 把批准的记录路由到选定归档目标。归档行只包含
   `Task`、`Takeaway` 和 `Improvement`。
6. **单独确认清理：**`done` 只显示汇总；只有后续 `confirm` 才能清理仍通过
   全部检查的 eligible source rows。

![工作流概览：记录、追踪、完成、复盘、整理并归档日常工作](docs/workflow-overview.svg)

_图中重点展示日常工作与完成后复盘闭环，并未呈现 `total YYYY-MM`
执行的每一次读取和校验。_

## 快速开始

前置条件：Git、Codex，以及可用的 Notion connector。

1. 克隆仓库并安装 Skill：

   ```sh
   git clone https://github.com/wensente9682/notion-work-organizer.git
   cd notion-work-organizer
   python3 -B skills/todo-archive-review/scripts/install.py
   ```

2. 新建 Codex 任务，并只为 Organize 应使用的页面和数据库启用 Notion connector。
3. 从当前可用的现有列表路径开始：

   **现有列表**

   ```text
   Use Organize with my existing Notion to-do list. Start read-only.
   ```

4. 同一 profile 的 readiness 检查通过后，先查看月份统计，再整理当月已完成工作：

   ```text
   total 2026-07
   organize todo
   ```

   `total` 只返回 Category block 小计和 overlapping grand total；它不展示任务
   明细，也不修改 Notion。
5. 检查每一次拟议写入。源记录清理始终位于独立的 `done` → `confirm` 安全门后。

可以在
[examples/session-output.example.txt](examples/session-output.example.txt)
查看去标识化的 Organize 复盘示例。

如果 Organize 帮助你建立了更有用的日常工作习惯，可以考虑为仓库点一个 star。

## Approval-First 安全原则

- 正常运行环境是 Codex + Notion connector。
- 现有列表 Adopt 从只读检查开始。
- 计划、检查、映射和 preview 都不授权 Notion 写入。
- 每一次 archive write 或本地私有 profile 变更，都需要针对该动作的明确批准。
  未来 New System provisioning 也必须保持同样的逐项批准边界。
- `ok` 和 `dismiss` 不移除源记录。`done` 只做汇总；只有单独的 `confirm`
  才能最终清理 eligible rows。
- 清理使用 Notion 可恢复的 archived 状态，而不是永久删除。
- Total 全程只读，不返回部分结果，也不会授予 Organize 任何批准。
- 公开示例不包含真实凭据、ID、URL、私有任务内容、用户名或本机路径。

## 当前边界

- Organize 在 Codex 中配合 Notion connector 运行，不提供单独应用或 GUI。
- New System provisioning 规划在 v0.3。当前版本不会创建或调整 database、
  field、view 或 archive target。
- Adopt 不会广泛扫描、去重、修复或维护无关的 Notion 内容。
- Total 只读取已配置 ordered view，只统计 completed items，不读取 archive，
  也不重建已被 Organize 清理的工作。
- Total 是调用时统计，不是保存的 dashboard 或历史报表系统。
- 归档记录有意省略 source-only workflow fields。
- Python CLI 是高级 fallback，而不是正常用户路径。

## 现有 Notion 列表路径

如果你已经有 Notion to-do list 或 work log，请使用这条路径。

1. 选择 source list 和相关 Category archive targets。
2. 让 Organize 以只读方式检查。
3. 查看去标识化的 `ready`、`missing` 和 `incompatible` 报告，覆盖：
   - source work fields；
   - 受支持的 Category routing 与 archive targets；
   - archive payload surface；
   - configured ordered view、structured date anchors 与 block field；
   - Total 和 Organize 需要的 mappings。
4. 确认拟议 mappings。
5. 把创建或更新 ignored private profile 作为单独本地动作批准。
6. 使用同一 profile 先做 Total 只读验证，再进入 Organize 只读 preview。

检查或 profile 批准都不授权 Notion mutation、archive write 或 source cleanup。
遇到含糊、不可访问、不完整、不受支持或外部已变更的输入时，流程安全停止。

## 新的 Notion 系统路径 — 规划于 v0.3

这是为尚无兼容日常工作系统的用户规划的未来路径。当前版本不能执行该路径，
也没有用于 provisioning workspace 的 Quick Start prompt 或命令。

v0.3 路径预计会：

- 从只读计划和 `ready` / `missing` 检查开始；
- 只在针对每个具体动作获得批准后，创建或调整 database、field、view、
  archive target 或配置；
- 只有同一个已批准 private profile 通过 Total 只读兼容性验证并进入 Organize
  只读 preview 后才完成。

在该能力实现并通过验收前，请对兼容的 Notion 工作系统使用现有列表路径。
未来的 readiness 仍不会授权创建 archive records 或清理 source。

## 必需 Schema

只要经过检查的映射得到端到端支持，实际字段标签可以不同。

### Source work list

| 角色 | 常见 Notion 类型 | 用途 |
| --- | --- | --- |
| `Task` | title | 日常工作项；复制到 archive `Task`。 |
| `Done` | checkbox | Total 与 Organize 的完成状态门。 |
| `Category` | 受支持的 select、multi-select、relation、status 或 mapped text | 路由统计归属和归档记录。 |
| `Takeaway` | rich text | 可选的可复用经验。 |
| `Improvement` | rich text | 可选的改进记录。 |
| Date anchor | structured date | 定义后续行继承的 ordered date section。 |
| Block field | rich text | 为 Total 保存一个非负 block 值。 |

Total 还需要一个明确配置的 ordered view。它遵循该 view 的保存顺序；无法验证
顺序、分页、字段结构或目标月份边界时会 fail closed。

### Category archive targets

| 角色 | 常见 Notion 类型 | 用途 |
| --- | --- | --- |
| `Task` | title | 从 source item 复制。 |
| `Takeaway` | rich text | 从 source item 复制。 |
| `Improvement` | rich text | 从 source item 复制。 |

Archive targets 不会镜像全部 source properties。

<details>
<summary><strong>高级配置与去标识化示例</strong></summary>

公开示例只使用占位值：

- [config.example.json](config.example.json)
- [real_profile.example.json](real_profile.example.json)
- [examples/](examples/)

Organize 配置示例：

```json
{
  "source_database_id": "YOUR_TODO_SOURCE_DATABASE_ID",
  "source_order": "bottom_first",
  "batch_size": 5,
  "move_limit": 30,
  "field_mapping": {
    "task": "Task",
    "done": "Done",
    "category": "Category",
    "takeaway": "Takeaway",
    "improvement": "Improvement"
  },
  "target_databases": {
    "example-category": "YOUR_EXAMPLE_CATEGORY_ARCHIVE_DATABASE_ID"
  }
}
```

Private profiles 应保存在被 Git 忽略的本地位置。它们可以包含 Total 和 Organize
所需且已验证的 source、archive、ordered-view 和 field mappings。不要把私有
profile、token 或真实 identifier 粘贴到聊天中或提交到仓库。

</details>

<details>
<summary><strong>Python CLI fallback 与 Keychain</strong></summary>

正常 Codex 使用可以跳过本节。Python CLI 用于回归测试、本地调试，或用户明确
要求的 Codex 外部运行。

在 Codex 外部运行时，凭据可以放在临时环境变量或 macOS Keychain 中，不得写入
仓库配置：

```sh
read -s NOTION_TOKEN
security add-generic-password -U -a "$USER" -s codex-notion-token -w "$NOTION_TOKEN"
unset NOTION_TOKEN
```

Fallback CLI 必须保持与正常 Codex 路径相同的批准、隐私和 fail-closed 边界。

</details>

<details>
<summary><strong>维护与本地 preflight 命令</strong></summary>

```sh
python3 notion_todo_workflow.py preflight
python3 notion_todo_workflow.py check
python3 notion_todo_workflow.py next
python3 notion_todo_workflow.py ok 1 3
python3 notion_todo_workflow.py dismiss 1
python3 notion_todo_workflow.py skip 2
python3 notion_todo_workflow.py done
python3 notion_todo_workflow.py confirm
python3 notion_todo_workflow.py status
python3 notion_todo_workflow.py undo 1
python3 setup_preflight.py --config config.example.json
```

本地 preflight 只读取本地 fixture，并报告配置或 schema gap。它不调用 Notion，
也不修改 workspace 内容。

</details>

## 测试与许可证

仓库维护 Setup、Adopt、Organize 和 Total 的 regression tests，并执行 Skill
validation。这里有意不固定测试数量；release evidence 应报告针对待验收候选
实际运行的检查。

本项目采用 [MIT License](LICENSE)。
