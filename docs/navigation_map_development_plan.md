# Navigation Map Development Plan

## 1. Goal

Build a map-first learning system for phone automation:

- Learn UI states and action transitions automatically.
- Reuse high-confidence transitions to reduce model calls.
- Keep strict safety boundaries for risky operations.
- Distill stable flows into shareable skills for community reuse.

This plan is designed for incremental delivery on top of current `ExperienceStore` + fast-path architecture.

## 2. Design Principles

1. Safety before speed.
2. Map first, policy second, skill packaging third.
3. Backward compatible with existing task execution path.
4. Online learning must be observable and reversible.
5. Use conservative defaults; enable aggressive behavior via explicit flags.

## 3. Architecture

### 3.1 Modules

1. `NavigationMapStore` (new)
- Persistent state graph (SQLite).
- State node upsert (`state_id`).
- Transition edge upsert (`from_state`, `to_state`, `action`).
- Confidence scoring for candidate actions.

2. `Agent Integration` (Android + iOS)
- Resolve pending transition when next state is observed.
- Query graph candidate action before model call.
- Gate graph fast-path with safety checks.
- Record decision source in token usage logs.

3. `Safety Layer`
- Action allowlist.
- Sensitive context block for `Tap/Type`.
- Minimum attempts and confidence thresholds.
- Consecutive-failure filters.
- Shared fast-path streak cap.

4. `Skill Compiler` (next phases)
- Mine repeated successful paths from graph.
- Generate parameterized skill package with tests + safety profile.

5. `Skill Registry` (next phases)
- Publish, version, score, rollback community skills.

### 3.2 Data Model (Phase 1)

`navigation_states`
- `state_id` (PK)
- `current_app`
- `screen_hash`
- `first_seen`, `last_seen`, `seen_count`

`state_transitions`
- `from_state_id`, `to_state_id`, `action_json` (composite PK)
- `attempts`, `successes`, `total_latency_ms`
- `last_outcome`, `consecutive_failures`, `updated_at`

## 4. Delivery Milestones

### M1 (this implementation)

- Add `NavigationMapStore`.
- Add graph learning hooks in Android/iOS agents.
- Add navigation fast-path gate (disabled by default, configurable).
- Add CLI/env configs and runtime header prints.

Acceptance:
- Compile passes.
- CLI help shows navigation options.
- At least one run writes/updates navigation DB.
- Decision source can show `navigation_fast_path`.

### M2

- Frontier exploration scheduler with per-task/app budgets.
- Return-then-explore strategy.
- Exploration reports (coverage, failure rate, risk blocks).

### M3

- Automatic skill extraction from high-confidence graph paths.
- Skill package format (`manifest + flow + tests + safety_profile`).
- Local verification pipeline before publish.

### M4

- Skill sharing and versioned registry.
- Reputation score based on success/cost/safety metrics.
- Safe rollout and rollback controls.

## 5. Safety Policy

1. Default exploration scope is app allowlist only.
2. Sensitive actions are blocked from autonomous fast-path by default.
3. Repeated failures trigger cooldown and confidence decay.
4. Every automated decision must be logged with source and outcome.

## 6. KPIs

1. Mean steps per task.
2. Mean token cost per task.
3. First-pass success rate.
4. Risk-action trigger rate.
5. Skill reuse rate (M3+).

## 7. Configuration Plan

Phase 1 introduces:

- `PHONE_AGENT_NAVIGATION_MAP_ENABLED`
- `PHONE_AGENT_NAVIGATION_FAST_PATH`
- `PHONE_AGENT_NAVIGATION_FAST_PATH_CONFIDENCE`
- `PHONE_AGENT_NAVIGATION_FAST_PATH_MIN_ATTEMPTS`

All defaults are conservative and can be tightened for high-risk environments.

## 8. Rollout Strategy

1. Enable map learning first (`navigation_map_enabled=true`, fast-path off).
2. Observe metrics and safety logs.
3. Enable navigation fast-path only after confidence baseline is stable.
4. Enable exploration scheduling in controlled windows (M2).

