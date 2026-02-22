[Task Skill: wechat-public-article]
When tasks involve WeChat public-account article search, extraction, or saving, follow this workflow:
Finish one-time startup normalization from `wechat-home-normalization` first, then run the flow below.

0. Entry normalization (mandatory once at startup)
- Before task actions, ensure WeChat is at its default home screen (bottom tabs visible, main "Chats" list active).
- If not currently in WeChat, launch WeChat first; if inside a deep WeChat page, go Back/close until home.
- Do not start searching/extracting public-account articles until home normalization is complete.
- After startup normalization is complete, do not force return-to-home again for this rule unless explicitly requested by user or the flow becomes unrecoverable.

1. Result filtering
- Prefer the "Articles" tab.
- The top category bar is horizontally scrollable; if "Articles" is not visible, swipe the category bar first and then select it.
- Skip cards marked as "Ad".
- Skip Channels, mini-programs, and shopping cards.
- Count only normal article cards with title + account + time metadata.

2. Navigation safety
- If you enter a mini-program/store page by mistake, immediately go Back.
- Skipped or mistaken entries must not be counted as completed.
- After configuring filters (sort/type/time/scope), prefer tapping a blank area to close the filter panel while keeping the applied filters; do not default to tapping "Cancel".

3. Extraction and saving
- Scroll at least once in each article before classification.
- For long articles, continue scrolling until near the bottom (comment/read/like/share area) or two consecutive no-change scrolls.
- For image-dominant content, prioritize "Save as image".
- For text-dominant content, capture title/source/time and summarize key points.
- For multi-article tasks, increase completion count only after confirmed success.

4. Mandatory extraction actions
- After entering an article, run: `do(action="Note", message="wechat_article_meta")`
- After each meaningful scroll, run: `do(action="Note", message="wechat_article_page")`
- When extraction is complete, run: `do(action="Call_API", instruction="wechat_export_to_download")`
- Only then call `finish(...)`.

5. Output constraints
- Keep reasoning concise; avoid repeating long page text.
- Emit exactly one valid action per step.
