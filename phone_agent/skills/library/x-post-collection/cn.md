[Task Skill: x-post-collection]
当任务涉及 X/Twitter 帖子采集、保存、导出（正文/日期/热度/评论/媒体类型）时，执行以下专用流程：

1. 入口与范围
- 先确认当前在 X（Twitter）应用内，再开始采集。
- 采集目标以“帖子详情页”为准，避免在列表页直接读数。
- 如误入系统浏览器、应用市场或无关 app，优先 Back 返回 X。

2. 多媒体识别规则
- 识别并记录媒体类型：`text` / `image` / `video` / `gif` / `quote` / `repost` / `link` / `poll`。
- 引用帖、转帖、回复帖均要保留类型标记，不强行当作纯文本帖。
- 若帖子仅有图片/视频且正文为空，`post_text` 可留空，不要臆造。
- 视频帖评论读取规则：不要在视频播放器层连续滑动；先按顺序点击“作者栏（头像/昵称）-> 正文/时间区域”尝试进入线程详情层，每个位置最多尝试 1 次，再读取评论。

3. 数据抓取顺序（每条帖子）
- 第一步：进入帖子详情页后立即执行  
  `do(action="Note", message="x_post_meta idx=N")`
- 第二步：若评论区可见，执行  
  `do(action="Note", message="x_post_comments idx=N")`
- 第三步：如需要查看更多评论，最多下滑 1 次（必须在屏幕中部滑动），再执行  
  `do(action="Note", message="x_post_comments idx=N")`
- 第四步：返回帖子列表页继续下一条。

4. 防卡死规则（强约束）
- 同一界面禁止重复同一动作超过 2 次。
- 若同一种尝试连续失败 3 次，立即停止当前条目并 `finish(message="need_takeover: reason")`，不要无限重试。
- 评论字段不可见时填 `null`，继续流程。
- 若视频帖 2 次点击后仍无法进入线程详情层，则直接将评论字段置 `null` 并继续下一条；若连续 3 次语义失败则 `finish(message="need_takeover: video_comment_entry_failed")`。

5. 批量导出协议（必须使用）
- 完成单条或批量后，执行：  
  `do(action="Call_API", instruction="x_export_to_download")`
- 导出后再 `finish(...)`，避免遗漏文件。

6. 输出约束
- 每步只输出一个合法动作。
- 推理简短，不复述整段帖子文本。
