[Task Skill: x-post-collection]
Use this workflow for X/Twitter post extraction tasks (post text, time, engagement, comments, media types).

1. Entry and scope
- Ensure the current app is X/Twitter before extraction.
- Treat post detail pages as the source of truth; do not rely on list-page snippets.
- If drifted to browser/app store/other apps, go Back to X first.

2. Mixed-media handling
- Detect and record media types: `text` / `image` / `video` / `gif` / `quote` / `repost` / `link` / `poll`.
- Keep repost/quote/reply flags instead of flattening everything into plain text posts.
- If text is not visible for image/video-only posts, keep `post_text` empty/null (do not hallucinate).
- Video comment rule: do not keep swiping inside media-player view; try entering thread-detail in order `author row -> post text/time area`, max one tap per target, then read comments.

3. Per-post capture protocol
- After entering post detail, run:  
  `do(action="Note", message="x_post_meta idx=N")`
- If comments are visible, run:  
  `do(action="Note", message="x_post_comments idx=N")`
- If more comments are needed, perform at most one mid-screen swipe, then run:  
  `do(action="Note", message="x_post_comments idx=N")`
- Return to the list and continue to next post.

4. Anti-stall constraints
- Do not repeat the same action on the same page more than 2 times.
- If the same method fails 3 consecutive times, stop that item and  
  `finish(message="need_takeover: reason")`.
- If a field is not visible, set it to `null` and proceed.
- If video thread-detail still cannot be entered after 2 taps, set comment fields to `null` and continue; if 3 semantic failures occur in a row, `finish(message="need_takeover: video_comment_entry_failed")`.

5. Mandatory export protocol
- After single-item or batch capture, run:  
  `do(action="Call_API", instruction="x_export_to_download")`
- Only then call `finish(...)`.

6. Output constraints
- Emit exactly one valid action per step.
- Keep reasoning concise; do not copy long post text into reasoning.
