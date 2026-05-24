"""Bot 自动发帖逻辑"""

from datetime import datetime

from src.storage import read_jsonl, append_jsonl, now_iso
from src.poster import create_post, generate_post

from src.core.utils import POSTS_SENT_FILE, log


def do_auto_post(session, config, prompt_path, dry_run=False):
    """自动发帖：检查是否到了发帖时间"""
    bot_config = config.get("bot", {})
    if not bot_config.get("post_enabled", False):
        return

    post_interval = bot_config.get("post_interval_hours", 24) * 3600
    topic_id = bot_config.get("post_topic_id", "7214")

    # 检查上次发帖时间
    sent_records = [r for r in read_jsonl(POSTS_SENT_FILE) if r.get("status") == "success"]
    if sent_records:
        last_time_str = max(r["posted_at"] for r in sent_records)
        try:
            last_time = datetime.fromisoformat(last_time_str)
            elapsed = (datetime.now() - last_time).total_seconds()
            if elapsed < post_interval:
                remaining = int(post_interval - elapsed)
                log(f"[发帖] 距下次发帖还需 {remaining // 3600}h{(remaining % 3600) // 60}m")
                return
        except (ValueError, TypeError):
            pass

    # 生成并发帖
    log("[发帖] 开始生成帖子...")
    try:
        title, content, hashtags = generate_post(config, prompt_path)
    except Exception as e:
        log(f"[发帖] 生成失败: {e}")
        return

    log(f"[发帖] 标题: {title}")
    log(f"[发帖] 标签: {hashtags}")
    log(f"[发帖] 正文 ({len(content)} 字)")

    if dry_run:
        log("[发帖] DRY RUN，未发送")
        append_jsonl(POSTS_SENT_FILE, {
            "title": title,
            "content": content,
            "topic_id": topic_id,
            "hashtags": hashtags,
            "status": "dry_run",
            "posted_at": now_iso(),
        })
        return

    try:
        result = create_post(session, config, title, content, topic_ids=topic_id, hashtags=hashtags)
        if result.get("status") == "ok":
            log(f"[发帖] 发送成功! link_id={result.get('link_id')}")
            append_jsonl(POSTS_SENT_FILE, {
                "link_id": str(result.get("link_id", "")),
                "title": title,
                "content": content,
                "topic_id": topic_id,
                "hashtags": hashtags,
                "status": "success",
                "posted_at": now_iso(),
            })
        else:
            log(f"[发帖] 发送失败: {result.get('msg') or result}")
            append_jsonl(POSTS_SENT_FILE, {
                "title": title,
                "content": content,
                "topic_id": topic_id,
                "hashtags": hashtags,
                "status": "failed",
                "error": result.get("msg") or str(result),
                "posted_at": now_iso(),
            })
    except Exception as e:
        log(f"[发帖] 发送出错: {e}")
