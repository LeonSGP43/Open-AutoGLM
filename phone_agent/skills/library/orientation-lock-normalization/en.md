[Task Skill: orientation-lock-normalization]
Before every task, run one-time orientation normalization: keep the device in portrait and disable auto-rotate, then continue task actions.

1. Startup hard constraint (must run first)
- Before any business action, check whether the UI is landscape/sideways or auto-rotate is enabled.
- If landscape is suspected, or an auto-rotate toggle appears enabled, fix orientation first.

2. Normalization steps (repeat until satisfied)
- Open Quick Settings/Control Center and find the rotation toggle (Auto-rotate / Portrait lock / Rotation lock).
- If auto-rotate is enabled, switch it to portrait lock (or equivalent disabled auto-rotate state).
- If portrait lock is already enabled, keep it and return to the task UI.
- Re-check that the UI is readable in portrait layout, then continue the task.

3. Failure handling
- If portrait cannot be recovered in 3 tries, use `do(action="Take_over", message="Please disable auto-rotate and restore portrait mode, then press Enter to continue")`.
- After takeover, re-check orientation, then continue.

4. Execution frequency
- Run this normalization once at task start.
- Do not repeat it later in the same task unless orientation breaks again and blocks progress.

5. Output constraint
- Emit one valid action per step; never merge normalization and business actions in one step.
