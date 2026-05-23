"""小黑盒 Bot — 自动抓取 + 自动回复，持续运行"""

import os
import signal
import threading
import time
from datetime import datetime, timedelta

import yaml

from src.commenter import post_comment, post_comment_reply
from src.llm import generate_comment, load_prompt
from src.scraper import get_session, fetch_post_links, fetch_post_detail, fetch_top_comments, _base_params
from src.storage import read_jsonl, append_jsonl, update_record, get_existing_ids, now_iso
from post import create_post, generate_post
from reply_at import fetch_at_messages

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
POSTS_FILE = os.path.join(DATA_DIR, "posts.jsonl")
AT_FILE = os.path.join(DATA_DIR, "at_messages.jsonl")
REPLY_FILE = os.path.join(DATA_DIR, "reply_messages.jsonl")
POSTS_SENT_FILE = os.path.join(DATA_DIR, "posts_sent.jsonl")
HOT_POSTS_FILE = os.path.join(DATA_DIR, "hot_posts.jsonl")
NEWS_SENT_FILE = os.path.join(DATA_DIR, "news_sent.jsonl")
ROLE_SWITCH_FILE = os.path.join(DATA_DIR, "role_switches.jsonl")

running = True


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def interruptible_sleep(seconds):
    """可中断的 sleep，每秒检查 running 状态"""
    for _ in range(int(seconds)):
        if not running:
            return
        time.sleep(1)


# ─── 抓取逻辑 ───────────────────────────────────────────────

def do_fetch_posts(session, config):
    """抓取新帖子，写入 posts.jsonl"""
    # 支持 topic_ids 列表或旧的 topic_id 单个值
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
            # 获取完整内容，每条间隔10秒防风控
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

            if not running:
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
        # 跳过缺少关键字段的消息
        if "linkid" not in msg or "comment_a_id" not in msg:
            continue

        # comment_a_text 是对方回复我的内容
        raw_text = msg.get("comment_a_text", "")
        clean_text = _re.sub(r"<[^>]+>", "", raw_text).strip()
        # comment_b_text 是我的原始评论
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
            # 获取完整内容
            try:
                detail = fetch_post_detail(session, config, post["link_id"])
                full_content = detail.get("content", post["description"])
            except Exception:
                full_content = post["description"]

            # 获取热门评论
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


def do_daily_news(session, config, prompt_path, dry_run=False):
    """每日新闻：到点后汇总热门帖子并发帖，支持多个时间点"""
    hot_config = config.get("hot_posts", {})
    if not hot_config.get("news_enabled", False):
        return

    # 支持 news_hours 列表或旧的 news_hour 单值
    news_hours = hot_config.get("news_hours", None)
    if not news_hours:
        news_hours = [hot_config.get("news_hour", 10)]
    news_hours = sorted(news_hours)

    now = datetime.now()

    # 找到当前应该触发的时间点：最近一个已过的 news_hour
    current_slot = None
    for h in news_hours:
        if now.hour >= h:
            current_slot = h
    if current_slot is None:
        return  # 还没到今天第一个发布时间

    # 检查这个时间段是否已发过
    news_records = read_jsonl(NEWS_SENT_FILE)
    today_str = now.strftime("%Y-%m-%d")
    for r in news_records:
        if r.get("status") == "success" and r.get("posted_at", "").startswith(today_str):
            # 检查是否是同一个时间段发的（同一小时段）
            try:
                posted = datetime.fromisoformat(r["posted_at"])
                if posted.hour >= current_slot:
                    # 找到下一个时间点
                    next_slot = None
                    for h in news_hours:
                        if h > current_slot:
                            next_slot = h
                            break
                    # 如果没有下一个时间点，或者发帖时间在当前slot范围内，说明已发过
                    if next_slot is None or posted.hour < next_slot:
                        return
            except (ValueError, KeyError):
                pass

    # 收集已上过新闻的帖子 link_id，避免重复
    used_link_ids = set()
    for r in news_records:
        if r.get("status") == "success":
            for lid in r.get("source_link_ids", []):
                used_link_ids.add(lid)

    # 确定素材时间窗口：上一个时间点到当前时间点
    # 找上一个时间点（可能是昨天的最后一个）
    slot_idx = news_hours.index(current_slot)
    if slot_idx == 0:
        # 第一个时间点，上一个是昨天的最后一个时间点
        prev_hour = news_hours[-1]
        cutoff_start = (now - timedelta(days=1)).replace(hour=prev_hour, minute=0, second=0, microsecond=0)
    else:
        prev_hour = news_hours[slot_idx - 1]
        cutoff_start = now.replace(hour=prev_hour, minute=0, second=0, microsecond=0)
    cutoff_end = now.replace(hour=current_slot, minute=0, second=0, microsecond=0)

    hot_posts = read_jsonl(HOT_POSTS_FILE)
    candidates = []
    seen_titles = set()
    for p in hot_posts:
        try:
            fetched = datetime.fromisoformat(p["fetched_at"])
        except (ValueError, KeyError):
            continue
        if cutoff_start <= fetched <= cutoff_end:
            # 去重：同标题 + 已上过新闻的
            if p["title"] not in seen_titles and p["link_id"] not in used_link_ids:
                candidates.append(p)
                seen_titles.add(p["title"])

    if not candidates:
        log("[新闻] 没有足够的热门帖子素材，跳过")
        return

    # 按评论数排序，取前 N 条
    max_items = hot_config.get("news_max_items", 15)
    candidates.sort(key=lambda x: x.get("comment_num", 0), reverse=True)
    top_posts = candidates[:max_items]

    # 构造 LLM prompt
    from openai import OpenAI
    llm_config = config["llm"]
    client = OpenAI(base_url=llm_config["base_url"], api_key=llm_config["api_key"])

    system_prompt, _ = load_prompt(prompt_path)

    news_prompt_path = os.path.join(os.path.dirname(prompt_path), "news.md")
    with open(news_prompt_path, "r", encoding="utf-8") as f:
        news_prompt = f.read().strip()

    # 拼接帖子列表（含热评）
    posts_parts = []
    for i, p in enumerate(top_posts):
        line = f"{i+1}. 【{p['title']}】({p.get('comment_num', 0)}条评论) — {(p.get('content') or p.get('description', ''))[:150]}"
        comments = p.get("top_comments", [])
        if comments:
            hot_comments = " | ".join(f"{c['text'][:40]}({c['up']}赞)" for c in comments)
            line += f"\n   热评: {hot_comments}"
        posts_parts.append(line)
    posts_text = "\n".join(posts_parts)
    user_text = f"{news_prompt}\n\n{posts_text}"

    log(f"[新闻] 正在生成每日快讯（{len(top_posts)} 条素材）...")
    try:
        response = client.chat.completions.create(
            model=llm_config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            max_tokens=1500,
            temperature=0.8,
        )
    except Exception as e:
        log(f"[新闻] LLM 生成失败: {e}")
        return

    usage = response.usage
    if usage:
        log(f"  [token] input={usage.prompt_tokens}, output={usage.completion_tokens}, total={usage.total_tokens}")

    result = response.choices[0].message.content.strip()
    result = result.replace('"', " ").replace("\u201c", " ").replace("\u201d", " ")

    # 正文就是 LLM 的完整输出（不再解析标题）
    content = result.strip()

    # 在每条帖子点评中，将短标题变成可点击链接
    # LLM 输出格式是 "序号. 短标题 — 点评"，把短标题包裹成 <a> 标签
    import re as _re
    lines = content.split("\n")
    new_lines = []
    for line in lines:
        m = _re.match(r"^(\d+)\.\s*(.+?)\s*[—–-]\s*(.+)$", line)
        if m:
            idx = int(m.group(1)) - 1
            short_title = m.group(2)
            comment = m.group(3)
            if 0 <= idx < len(top_posts):
                p = top_posts[idx]
                link = f'<a href="https://www.xiaoheihe.cn/app/bbs/link/{p["link_id"]}" target="_blank">{short_title}</a>'
                new_lines.append(f"{m.group(1)}. {link} — {comment}")
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)
    content = "\n".join(new_lines)

    # 生成标题：日期 + 周几 + 期数
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday = weekdays[now.weekday()]
    # 期数 = 已成功发布的新闻数 + 1
    success_count = sum(1 for r in news_records if r.get("status") == "success")
    issue_num = success_count + 1
    title = f"{now.strftime('%Y年%m月%d日')} {weekday} 每日热点（第{issue_num}期）"

    # 转换为 HTML 格式
    html_lines = []
    for line in content.split("\n"):
        if line.strip():
            html_lines.append(f"<p>{line}</p>")
        else:
            html_lines.append("<p><br/></p>")
    content = "".join(html_lines)

    log(f"[新闻] 标题: {title}")
    log(f"[新闻] 正文 ({len(content)} 字)")

    if dry_run:
        log("[新闻] DRY RUN，未发送")
        append_jsonl(NEWS_SENT_FILE, {
            "title": title,
            "content": content,
            "post_count": len(top_posts),
            "status": "dry_run",
            "posted_at": now_iso(),
        })
        return

    topic_id = hot_config.get("news_topic_id", "7214")
    try:
        result_resp = create_post(session, config, title, content, topic_ids=topic_id, hashtags=["每日热点"], use_html=True)
        if result_resp.get("status") == "ok":
            log(f"[新闻] 发送成功! link_id={result_resp.get('link_id')}")
            append_jsonl(NEWS_SENT_FILE, {
                "link_id": str(result_resp.get("link_id", "")),
                "title": title,
                "content": content,
                "post_count": len(top_posts),
                "source_link_ids": [p["link_id"] for p in top_posts],
                "status": "success",
                "posted_at": now_iso(),
            })
        else:
            log(f"[新闻] 发送失败: {result_resp.get('msg') or result_resp}")
            append_jsonl(NEWS_SENT_FILE, {
                "title": title,
                "content": content,
                "post_count": len(top_posts),
                "status": "failed",
                "error": result_resp.get("msg") or str(result_resp),
                "posted_at": now_iso(),
            })
    except Exception as e:
        log(f"[新闻] 发送出错: {e}")


def fetch_loop(session, config, prompt_path, dry_run=False):
    """定时抓取线程：按优先级依次抓取 @消息 → 回复评论 → 新帖子 → 自动发帖"""
    bot_config = config.get("bot", {})
    fetch_interval = bot_config.get("fetch_interval", 300)
    fetch_cooldown = bot_config.get("fetch_cooldown", 10)

    while running:
        # 优先级1：抓取@消息
        do_fetch_at(session, config)
        interruptible_sleep(fetch_cooldown)
        if not running:
            break

        # 优先级2：抓取回复评论
        do_fetch_replies(session, config)
        interruptible_sleep(fetch_cooldown)
        if not running:
            break

        # 优先级3：抓取新帖子
        do_fetch_posts(session, config)
        interruptible_sleep(fetch_cooldown)
        if not running:
            break

        # 自动发帖（检查是否到时间）
        do_auto_post(session, config, prompt_path, dry_run)
        interruptible_sleep(fetch_cooldown)
        if not running:
            break

        # 抓取热门帖子
        do_fetch_hot_posts(session, config)
        interruptible_sleep(fetch_cooldown)
        if not running:
            break

        # 每日新闻（检查是否到发布时间）
        do_daily_news(session, config, prompt_path, dry_run)

        # 等待下一轮
        log(f"[抓取] 下一轮抓取在 {fetch_interval} 秒后")
        interruptible_sleep(fetch_interval)


# ─── 回复逻辑 ───────────────────────────────────────────────

def get_pending_posts():
    """获取待回复的帖子（没有 status=success 的）"""
    posts = read_jsonl(POSTS_FILE)
    return [p for p in posts if p.get("status") != "success"]


def reply_to_post(session, config, prompt, post, dry_run=False):
    """回复一个帖子"""
    link_id = post["link_id"]
    log(f"[回复帖子] {link_id} - {post.get('title', '')[:40]}")

    # 如果已有生成的评论（上次发送失败），直接复用
    comment = post.get("pending_comment")
    if comment:
        log(f"[回复帖子] 复用已生成: {comment}")
    else:
        comment = generate_comment(config, prompt, post)
        log(f"[回复帖子] 生成: {comment}")

    if not dry_run:
        result = post_comment(session, config, link_id, comment)
        if result.get("status") == "ok":
            log(f"[回复帖子] 发送成功 (commentid={result.get('commentid')})")
        else:
            msg_text = result.get("msg") or str(result)
            log(f"[回复帖子] 发送失败: {msg_text}")
            if "屏蔽" in msg_text or "违规" in msg_text:
                # 屏蔽词导致的失败，清掉缓存让下次重新生成
                log("[回复帖子] 检测到屏蔽词，将重新生成")
                update_record(POSTS_FILE, link_id, {"pending_comment": None})
            else:
                # 其他失败（网络等），保存已生成的评论下次重发
                update_record(POSTS_FILE, link_id, {"pending_comment": comment})
            return

    update_record(POSTS_FILE, link_id, {
        "comment": comment,
        "status": "success",
        "commented_at": now_iso(),
        "pending_comment": None,  # 清除待发送标记
    })


def reply_to_at(session, config, prompt, msg, dry_run=False):
    """回复一条@消息"""
    log(f"[回复@] 来自 {msg['user_a']} - {msg['comment_text'][:40]}")

    # 获取帖子详情作为上下文
    try:
        post_detail = fetch_post_detail(session, config, msg["link_id"])
    except Exception:
        post_detail = {"title": msg["link_title"], "content": "", "imgs": []}

    # 单独构造@回复的 prompt
    system_prompt = prompt[0]

    user_text = (
        f"帖子标题：{post_detail.get('title') or msg['link_title']}\n\n"
        f"帖子内容：{post_detail.get('content', '')[:300]}\n\n"
        f"---\n"
        f"用户「{msg['user_a']}」在这个帖子的评论区@了我，说：「{msg['comment_text']}」\n\n"
        f"请你回复这个@我的用户。要求：\n"
        f"- 针对对方说的内容回应\n"
        f"- 保持你的人设和语气\n"
        f"- 只输出回复内容，不超过30字"
    )

    from openai import OpenAI
    llm_config = config["llm"]
    client = OpenAI(base_url=llm_config["base_url"], api_key=llm_config["api_key"])

    response = client.chat.completions.create(
        model=llm_config["model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        max_tokens=200,
        temperature=0.8,
    )

    usage = response.usage
    if usage:
        print(f"  [token] input={usage.prompt_tokens}, output={usage.completion_tokens}, total={usage.total_tokens}")

    comment = response.choices[0].message.content.strip()
    comment = comment.replace('"', " ").replace("\u201c", " ").replace("\u201d", " ")
    comment = " ".join(comment.split())
    log(f"[回复@] 生成: {comment}")

    if not dry_run:
        result = post_comment_reply(
            session, config,
            link_id=msg["link_id"],
            text=comment,
            reply_id=msg["comment_a_id"],
            root_id=msg["root_comment_id"],
        )
        if result.get("status") == "ok":
            log(f"[回复@] 发送成功 (commentid={result.get('commentid')})")
        else:
            log(f"[回复@] 发送失败: {result.get('msg') or result}")
            return  # 发送失败不标记，下次重试

    append_jsonl(AT_FILE, {
        "message_id": msg["message_id"],
        "link_id": msg["link_id"],
        "user_a": msg["user_a"],
        "comment_text": msg["comment_text"],
        "reply": comment,
        "status": "dry_run" if dry_run else "success",
        "replied_at": now_iso(),
    })


def reply_to_reply(session, config, prompt, msg, dry_run=False):
    """回复一条'回复我的评论'"""
    log(f"[回复评论] 来自 {msg['user_a']} - {msg['comment_text'][:40]}")

    # 检测"转xx"角色切换
    role_switch_dir = config.get("role_switch_dir", "prompts/persona")
    role_whitelist = config.get("role_whitelist", [])
    comment_text = msg["comment_text"]
    switched_role = None

    # 用白名单构造正则，精确匹配"转+角色名"
    import re as _re
    switched_role = None
    if role_whitelist:
        # 按长度降序排列，优先匹配长的（如"假面骑士"优先于"假面"）
        sorted_roles = sorted(role_whitelist, key=len, reverse=True)
        pattern = r"转(" + "|".join(_re.escape(r) for r in sorted_roles) + r")"
        role_match = _re.search(pattern, comment_text)
        if role_match:
            switched_role = role_match.group(1)
        else:
            # 检查是否有"转xx"意图但不在白名单
            general_match = _re.search(r"转([\u4e00-\u9fffa-zA-Z]{1,6})", comment_text)
            if general_match:
                log(f"[角色切换] 不在白名单，忽略: 转{general_match.group(1)}")

    # 如果本次检测到了新的角色切换，记录下来
    if switched_role:
        role_records = [r for r in read_jsonl(ROLE_SWITCH_FILE)
                        if r.get("link_id") == msg["link_id"]
                        and r.get("user") == msg["user_a"]]
        last_role = role_records[-1].get("role") if role_records else None
        if switched_role != last_role:
            append_jsonl(ROLE_SWITCH_FILE, {
                "link_id": msg["link_id"],
                "user": msg["user_a"],
                "role": switched_role,
                "switched_at": now_iso(),
            })
            log(f"[角色切换] 用户 {msg['user_a']} 触发: 转{switched_role}")

    # 查找当前用户是否有角色切换记录（包括刚记录的）
    if not switched_role:
        role_records = [r for r in read_jsonl(ROLE_SWITCH_FILE)
                        if r.get("link_id") == msg["link_id"]
                        and r.get("user") == msg["user_a"]]
        if role_records:
            switched_role = role_records[-1].get("role")

    # 确定使用的 system_prompt
    if switched_role:
        # 从 prompts/persona/xx.md 读取人设
        persona_path = os.path.join(role_switch_dir, f"{switched_role}.md")
        if os.path.exists(persona_path):
            with open(persona_path, "r", encoding="utf-8") as f:
                persona_text = f.read().strip()
            log(f"[角色切换] 使用角色文件: {persona_path}")
        else:
            # 白名单里有但文件不存在，用 _fallback.md 兜底
            fallback_path = os.path.join(role_switch_dir, "_fallback.md")
            if os.path.exists(fallback_path):
                with open(fallback_path, "r", encoding="utf-8") as f:
                    persona_text = f.read().strip().replace("{{role_name}}", switched_role)
            else:
                persona_text = f"你现在扮演「{switched_role}」这个角色。用符合这个角色身份的语气、口癖和性格来回复。回复要简短自然，不超过30字。"
            log(f"[角色切换] 文件缺失，使用兜底模板: {switched_role}")
        # 拼上 base.md（表情包 + 回复模板）
        base_path = os.path.join("prompts", "base.md")
        with open(base_path, "r", encoding="utf-8") as f:
            base_text = f.read().strip()
        combined = f"{persona_text}\n\n{base_text}"
        if "========" in combined:
            system_prompt = combined.split("========", 1)[0].strip()
        else:
            system_prompt = combined
    else:
        system_prompt = prompt[0]

    # 获取帖子详情作为上下文
    try:
        post_detail = fetch_post_detail(session, config, msg["link_id"])
    except Exception:
        post_detail = {"title": msg["link_title"], "content": ""}

    # 聚合同一帖子下与同一用户的对话历史
    max_rounds = config.get("bot", {}).get("reply_history_rounds", 10)
    history_records = [r for r in read_jsonl(REPLY_FILE)
                       if r.get("link_id") == msg["link_id"]
                       and r.get("user_a") == msg["user_a"]
                       and r.get("status") == "success"]
    history_records.sort(key=lambda r: r.get("replied_at", ""))

    # 构造对话历史文本
    history_text = ""
    if history_records:
        history_lines = []
        for r in history_records[-max_rounds:]:  # 最多取最近5轮
            history_lines.append(f"对方：{r['comment_text']}")
            history_lines.append(f"我：{r['reply']}")
        history_text = "\n".join(history_lines)

    # 构造回复评论的 user prompt

    if history_text:
        user_text = (
            f"帖子标题：{post_detail.get('title') or msg['link_title']}\n\n"
            f"帖子内容：{post_detail.get('content', '')[:300]}\n\n"
            f"---\n"
            f"我和用户「{msg['user_a']}」在这个帖子下的对话历史：\n"
            f"{history_text}\n\n"
            f"现在对方又回复我说：「{msg['comment_text']}」\n\n"
            f"请你继续这个对话，回复对方。要求：\n"
            f"- 针对对方最新说的内容回应\n"
            f"- 不要重复之前说过的话\n"
            f"- 保持你的人设和语气\n"
            f"- 只输出回复内容，不超过30字"
        )
    else:
        user_text = (
            f"帖子标题：{post_detail.get('title') or msg['link_title']}\n\n"
            f"帖子内容：{post_detail.get('content', '')[:300]}\n\n"
            f"---\n"
            f"我之前在这个帖子下评论了：「{msg['my_comment']}」\n"
            f"然后用户「{msg['user_a']}」回复我说：「{msg['comment_text']}」\n\n"
            f"请你继续这个对话，回复对方。要求：\n"
            f"- 针对对方说的内容回应，不要重复我之前说过的话\n"
            f"- 保持你的人设和语气\n"
            f"- 只输出回复内容，不超过30字"
        )

    from openai import OpenAI
    llm_config = config["llm"]
    client = OpenAI(base_url=llm_config["base_url"], api_key=llm_config["api_key"])

    response = client.chat.completions.create(
        model=llm_config["model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        max_tokens=200,
        temperature=0.8,
    )

    usage = response.usage
    if usage:
        print(f"  [token] input={usage.prompt_tokens}, output={usage.completion_tokens}, total={usage.total_tokens}")

    comment = response.choices[0].message.content.strip()
    comment = comment.replace('"', " ").replace("\u201c", " ").replace("\u201d", " ")
    comment = " ".join(comment.split())
    log(f"[回复评论] 生成: {comment}")

    if not dry_run:
        result = post_comment_reply(
            session, config,
            link_id=msg["link_id"],
            text=comment,
            reply_id=msg["comment_a_id"],
            root_id=msg["root_comment_id"],
        )
        if result.get("status") == "ok":
            log(f"[回复评论] 发送成功 (commentid={result.get('commentid')})")
        else:
            log(f"[回复评论] 发送失败: {result.get('msg') or result}")
            return  # 发送失败不标记，下次重试

    append_jsonl(REPLY_FILE, {
        "message_id": msg["message_id"],
        "link_id": msg["link_id"],
        "user_a": msg["user_a"],
        "comment_text": msg["comment_text"],
        "my_comment": msg["my_comment"],
        "reply": comment,
        "status": "dry_run" if dry_run else "success",
        "replied_at": now_iso(),
    })


def reply_loop(session, config, prompt, dry_run=False):
    """主线程：逐条回复，按冷却时间间隔。优先级：@消息 > 回复评论 > 帖子"""
    bot_config = config.get("bot", {})
    cooldown = bot_config.get("reply_cooldown", 180)

    while running:
        # 1. 获取待回复的@消息
        replied_at_ids = {r["message_id"] for r in read_jsonl(AT_FILE) if "message_id" in r}
        try:
            at_messages = fetch_at_messages(session, config)
            cutoff = (datetime.now() - timedelta(hours=config.get("bot", {}).get("max_age_hours", 24))).timestamp()
            pending_at = [m for m in at_messages
                         if m["message_id"] not in replied_at_ids
                         and m["timestamp"] >= cutoff]
        except Exception:
            pending_at = []

        # 2. 获取待回复的"回复我的评论"
        replied_reply_ids = {r["message_id"] for r in read_jsonl(REPLY_FILE) if "message_id" in r}
        try:
            reply_messages = fetch_reply_messages(session, config)
            cutoff = (datetime.now() - timedelta(hours=config.get("bot", {}).get("max_age_hours", 24))).timestamp()
            pending_replies = [m for m in reply_messages
                              if m["message_id"] not in replied_reply_ids
                              and m["timestamp"] >= cutoff]
        except Exception:
            pending_replies = []

        # 3. 获取待回复的帖子
        pending_posts = get_pending_posts()

        total_pending = len(pending_at) + len(pending_replies) + len(pending_posts)
        if total_pending == 0:
            log("[回复] 无待回复内容，等待中...")
            for _ in range(10):
                if not running:
                    return
                time.sleep(1)
            continue

        log(f"[回复] 待回复: {len(pending_at)} @消息, {len(pending_replies)} 回复评论, {len(pending_posts)} 帖子")

        replied_count = 0

        # 优先级1：回复@消息
        for msg in pending_at:
            if not running:
                return
            try:
                reply_to_at(session, config, prompt, msg, dry_run)
                replied_count += 1
            except Exception as e:
                log(f"[回复@] 出错: {e}")
            log(f"[冷却] 等待 {cooldown} 秒...")
            interruptible_sleep(cooldown)

        # 优先级2：回复"回复我的评论"
        for msg in pending_replies:
            if not running:
                return
            try:
                reply_to_reply(session, config, prompt, msg, dry_run)
                replied_count += 1
            except Exception as e:
                log(f"[回复评论] 出错: {e}")
            log(f"[冷却] 等待 {cooldown} 秒...")
            interruptible_sleep(cooldown)

        # 优先级3：回复帖子（每回复几条检查一次高优先级消息）
        check_every = bot_config.get("check_priority_every", 3)
        post_count = 0
        for post in pending_posts:
            if not running:
                return
            # 每回复 check_every 条帖子，检查是否有新的@或回复评论
            if post_count > 0 and post_count % check_every == 0:
                try:
                    new_at = fetch_at_messages(session, config)
                    new_at_ids = {r["message_id"] for r in read_jsonl(AT_FILE) if "message_id" in r}
                    cutoff = (datetime.now() - timedelta(hours=bot_config.get("max_age_hours", 24))).timestamp()
                    urgent_at = [m for m in new_at if m["message_id"] not in new_at_ids and m["timestamp"] >= cutoff]
                except Exception:
                    urgent_at = []
                try:
                    new_replies = fetch_reply_messages(session, config)
                    new_reply_ids = {r["message_id"] for r in read_jsonl(REPLY_FILE) if "message_id" in r}
                    urgent_replies = [m for m in new_replies if m["message_id"] not in new_reply_ids and m["timestamp"] >= cutoff]
                except Exception:
                    urgent_replies = []

                if urgent_at or urgent_replies:
                    log(f"[回复] 发现高优先级消息: {len(urgent_at)} @, {len(urgent_replies)} 回复，优先处理")
                    for msg in urgent_at:
                        if not running:
                            return
                        try:
                            reply_to_at(session, config, prompt, msg, dry_run)
                            replied_count += 1
                        except Exception as e:
                            log(f"[回复@] 出错: {e}")
                        interruptible_sleep(cooldown)
                    for msg in urgent_replies:
                        if not running:
                            return
                        try:
                            reply_to_reply(session, config, prompt, msg, dry_run)
                            replied_count += 1
                        except Exception as e:
                            log(f"[回复评论] 出错: {e}")
                        interruptible_sleep(cooldown)

            try:
                reply_to_post(session, config, prompt, post, dry_run)
                replied_count += 1
            except Exception as e:
                log(f"[回复帖子] 出错: {e}")
            log(f"[冷却] 等待 {cooldown} 秒...")
            interruptible_sleep(cooldown)
            post_count += 1

        log(f"[回复] 本轮完成，共回复 {replied_count} 条")


# ─── 入口 ────────────────────────────────────────────────────

def main():
    global running

    import argparse
    parser = argparse.ArgumentParser(description="小黑盒 Bot 自动运行")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="试运行，不实际发送")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    prompt_path = config.get("prompt_file", "prompts/warm.md")
    prompt = load_prompt(prompt_path)
    session = get_session(config)

    bot_config = config.get("bot", {})
    log("=" * 50)
    log("小黑盒 Bot 启动")
    log(f"  抓取间隔: {bot_config.get('fetch_interval', 300)}s")
    log(f"  抓取冷却: {bot_config.get('fetch_cooldown', 10)}s")
    log(f"  回复冷却: {bot_config.get('reply_cooldown', 180)}s")
    log(f"  抓取顺序: @消息 → 回复评论 → 新帖子")
    log(f"  回复优先级: @消息 > 回复评论 > 帖子")
    if bot_config.get("post_enabled", False):
        log(f"  自动发帖: 开启 (间隔 {bot_config.get('post_interval_hours', 24)}h)")
    else:
        log(f"  自动发帖: 关闭")
    hot_config = config.get("hot_posts", {})
    if hot_config.get("enabled", False):
        log(f"  热门抓取: 开启 (评论>={hot_config.get('min_comment_num', 10)})")
    if hot_config.get("news_enabled", False):
        news_hours = hot_config.get("news_hours", [hot_config.get("news_hour", 10)])
        hours_str = "、".join(f"{h}:00" for h in news_hours)
        log(f"  每日新闻: 开启 (每天 {hours_str} 发布)")
    if args.dry_run:
        log("  模式: DRY RUN（不实际发送）")
    log("=" * 50)

    # 优雅退出
    def handle_signal(sig, frame):
        global running
        log("\n收到退出信号，正在停止...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # 启动抓取线程
    fetcher = threading.Thread(target=fetch_loop, args=(session, config, prompt_path, args.dry_run), daemon=True)
    fetcher.start()

    # 主线程做回复
    try:
        reply_loop(session, config, prompt, dry_run=args.dry_run)
    except KeyboardInterrupt:
        pass

    log("Bot 已停止")


if __name__ == "__main__":
    main()
