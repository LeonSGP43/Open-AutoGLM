[Task Skill: wechat-home-normalization]
For WeChat-related tasks, perform "home normalization" once at task start, then continue task actions.

1. Home criteria (all must be true)
- WeChat bottom tabs are visible (Chats/Contacts/Discover/Me).
- Current page is the main "Chats" list, not an article/chat/search/channels detail page.

2. Startup normalization steps (mandatory until criteria are met)
- If not in WeChat: run `do(action="Launch", app="WeChat")`.
- If in WeChat but deep inside pages: use `do(action="Back")` or close buttons to return level by level.
- If tabs are visible but not on "Chats": tap the "Chats" tab first.

3. Hard constraints
- Do not perform search, messaging, public-account entry, channels entry, or extraction before home criteria is satisfied.
- If 5 consecutive backs still fail, switch to the visible "Chats" tab and continue normalization.
- Once normalization is done, do not force a return to home again in the same task unless the user explicitly requests it or the current flow is clearly unrecoverable.

4. Output constraints
- Emit one valid action per step; never merge normalization and task actions in one step.
