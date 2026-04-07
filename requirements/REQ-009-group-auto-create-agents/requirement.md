# REQ-009: Group Auto-Create Agents

## Status: RequirementDraft
## Created: 2026-04-07

## Background

When creating a group, users currently must pre-create agents individually and then select them from a checkbox list. This is tedious. The new flow auto-creates a full set of standard-role agents when a group is created — named `{group} - {role}` and sharing a single working directory.

## Functional Requirements

### F-01: Auto-create standard role agents on group creation
- Roles: product_manager, tech_director, developer, tester, user (skip custom)
- Name format: `{group_name} - {role display_name}` (e.g. "sprint-01 - Developer")
- system_prompt and topic_subscriptions loaded from current Role Templates

### F-02: Unified working directory input
- Single working directory field in NewGroupDialog (with PathSuggester)
- All auto-created agents share this directory
- Individual agents editable afterwards

### F-03: Dialog simplification
- NewGroupDialog: Group Name + Working Directory only (remove agent checkbox list)
- Return type changes to `(group_name, working_dir)`

## Acceptance Criteria

| AC | Description |
|:---|:---|
| AC-01 | Dialog shows Group Name + Working Directory inputs |
| AC-02 | Working Directory supports Tab autocomplete |
| AC-03 | Create produces 5 agents (PM, Tech Dir, Dev, Tester, User) |
| AC-04 | Agent names follow `{group} - {display_name}` |
| AC-05 | system_prompt + topics from Role Templates |
| AC-06 | All 5 agents added as group members |
| AC-07 | Non-existent path shows error toast, blocks creation |

## Change Log
| Version | Date | Changes |
|:---|:---|:---|
| v1 | 2026-04-07 | Initial |
