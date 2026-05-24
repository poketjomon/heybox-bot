"""Bot 抓取逻辑"""

import time
from datetime import datetime, timedelta

from src.scraper import fetch_post_links, fetch_post_detail, fetch_top_comments, _base_params, fetch_at_messages
from src.storage import read_jsonl, append_jsonl, get_existing_ids, now_iso

from src.core import utils as bot_utils
from src.core.utils import (
    POSTS_FILE, AT_FILE, REPLY_FILE, HOT_POSTS_FILE,
    log,
)


def do_fetch_posts(session, config):
    """抓取新帖子，写入 posts.jsonl"""
    topic_ids = config.get("topic_ids", None)
    if not topic_ids:
        topic_ids = [config.get("topic_id", "7214")]

    limit = config.get("fetch_limit", 10)
    sort_filter = config.get("sort_filter", "new")
    existing_ids = get_existing_ids(POSTS_FILE)
    max_comment_num = config.get("max_comment_num", 20)
    max_imgs = config.get("max_imgs", 4)

    total_new = 0
    for topic_id in topic_ids:
        try:
            posts = fetch_post_links(session, config, topic_id=topic_id, limit=limit, sort_filter=sort_filter)
        except Exception as e:
            log(f"[抓取] 话题 {topic_id} 获取失败: {e}")
            continue

        new_posts = [p for p in posts
                     if p["link_id"] not in existing_ids
                     and p.get("comment_num", 0) < max_comment_num
                     and len(p.get("imgs", [])) <= max_imgs]

        for post in new_posts:
            try:
                detail = fetch_post_detail(session, config, post["link_id"])
                full_content = detail.get("content", post["description"])
                full_imgs = detail.get("imgs", post.get("imgs", []))
            except Exception:
                full_content = post["description"]
                full_imgs = post.get("imgs", [])

            append_jsonl(POSTS_FILE, {
                "link_id": post["link_id"],
                "title": post["title"],
                "content": full_content,
                "imgs": full_imgs,
                "comment_num": post.get("comment_num", 0),
                "topic_id": topic_id,
                "fetched_at": now_iso(),
            })
            existing_ids.add(post["link_id"])
            log(f"  + [{post['link_id']}] {post['title'][:40]}")

            if not bot_utils.running:
                return
            time.sleep(10)

        total_new += len(new_posts)

    if total_new:
        log(f"[抓取] 新增 {total_new} 个帖子（{len(topic_ids)} 个话题）")
    else:
        log("[抓取] 无新帖子")


def do_fetch_at(session, config):
    """获取@消息"""
    try:
        messages = fetch_at_messages(session, config)
    except Exception as e:
        log(f"[抓取] 获取@消息失败: {e}")
        return

    cutoff = (datetime.now() - timedelta(hours=config.get("bot", {}).get("max_age_hours", 24))).timestamp()
    messages = [m for m in messages if m["timestamp"] >= cutoff]

    replied_ids = {r["message_id"] for r in read_jsonl(AT_FILE) if "message_id" in r}
    new_msgs = [m for m in messages if m["message_id"] not in replied_ids]

    if new_msgs:
        log(f"[抓取] 发现 {len(new_msgs)} 条新@消息")
    else:
        log("[抓取] 无新@消息")


def fetch_reply_messages(session, config, limit=20):
    """获取回复我的评论列表"""
    import re as _re
    url = "https://api.xiaoheihe.cn/bbs/app/user/message"
    params = _base_params(config, "/bbs/app/user/message")
    params.update({
        "list_type": "0",
        "offset": "0",
        "limit": str(limit),
        "no_more": "false",
    })

    resp = session.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()

    messages = []
    for msg in data.get("result", {}).get("messages", []):
        if "linkid" not in msg or "comment_a_id" not in msg:
            continue

        raw_text = msg.get("comment_a_text", "")
        clean_text = _re.sub(r"<[^>]+>", "", raw_text).strip()
        my_text = msg.get("comment_b_text", "")

        messages.append({
            "message_id": str(msg["message_id"]),
            "link_id": str(msg["linkid"]),
            "link_title": msg.get("link_title", ""),
            "comment_a_id": str(msg["comment_a_id"]),
            "root_comment_id": str(msg["root_comment_id"]),
            "comment_text": clean_text,
            "my_comment": my_text,
            "user_a": msg.get("user_a", {}).get("username", ""),
            "userid_a": str(msg.get("userid_a", "")),
            "timestamp": float(msg.get("timestamp", 0)),
        })
    return messages


def do_fetch_replies(session, config):
    """获取回复我的评论"""
    try:
        messages = fetch_reply_messages(session, config)
    except Exception as e:
        log(f"[抓取] 获取回复消息失败: {e}")
        return

    cutoff = (datetime.now() - timedelta(hours=config.get("bot", {}).get("max_age_hours", 24))).timestamp()
    messages = [m for m in messages if m["timestamp"] >= cutoff]

    replied_ids = {r["message_id"] for r in read_jsonl(REPLY_FILE) if "message_id" in r}
    new_msgs = [m for m in messages if m["message_id"] not in replied_ids]

    if new_msgs:
        log(f"[抓取] 发现 {len(new_msgs)} 条新回复")
    else:
        log("[抓取] 无新回复")


def do_fetch_hot_posts(session, config):
    """抓取热门帖子，存入 hot_posts.jsonl"""
    hot_config = config.get("hot_posts", {})
    if not hot_config.get("enabled", False):
        return

    topic_ids = config.get("topic_ids", None)
    if not topic_ids:
        topic_ids = [config.get("topic_id", "7214")]
    fetch_limit = hot_config.get("fetch_limit", 20)
    min_comment_num = hot_config.get("min_comment_num", 10)
    existing_ids = get_existing_ids(HOT_POSTS_FILE)

    total_new = 0
    for topic_id in topic_ids:
        try:
            posts = fetch_post_links(session, config, topic_id=topic_id, limit=fetch_limit, sort_filter="")
        except Exception as e:
            log(f"[热门] 话题 {topic_id} 获取失败: {e}")
            continue

        new_posts = [p for p in posts
                     if p["link_id"] not in existing_ids
                     and p.get("comment_num", 0) >= min_comment_num]
        for post in new_posts:
            try:
                detail = fetch_post_detail(session, config, post["link_id"])
                full_content = detail.get("content", post["description"])
            except Exception:
                full_content = post["description"]

            try:
                top_comments = fetch_top_comments(session, config, post["link_id"], limit=hot_config.get("top_comments", 6))
            except Exception:
                top_comments = []

            append_jsonl(HOT_POSTS_FILE, {
                "link_id": post["link_id"],
                "title": post["title"],
                "description": post["description"],
                "content": full_content,
                "top_comments": top_comments,
                "comment_num": post.get("comment_num", 0),
                "topic_id": topic_id,
                "fetched_at": now_iso(),
            })
            existing_ids.add(post["link_id"])

        total_new += len(new_posts)

    if total_new:
        log(f"[热门] 新增 {total_new} 条热门帖子")
    else:
        log("[热门] 无新热门帖子")
