"""小黑盒 Bot — 自动抓取 + 自动回复，持续运行"""

import os
import signal
import threading
import time
from datetime import datetime, timedelta

import yaml

from src.commenter import post_comment, post_comment_reply
from src.llm import generate_comment, load_prompt
from src.scraper import get_session, fetch_post_links, fetch_post_detail, _base_params
from src.storage import read_jsonl, append_jsonl, update_record, get_existing_ids, now_iso
from reply_at import fetch_at_messages

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
POSTS_FILE = os.path.join(DATA_DIR, "posts.jsonl")
AT_FILE = os.path.join(DATA_DIR, "at_messages.jsonl")
REPLY_FILE = os.path.join(DATA_DIR, "reply_messages.jsonl")

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


def fetch_loop(session, config):
    """定时抓取线程：按优先级依次抓取 @消息 → 回复评论 → 新帖子，每次请求之间冷却"""
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
            log(f"[回复帖子] 发送失败: {result.get('msg') or result}")
            # 保存已生成的评论，下次直接重发
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

    # 获取帖子详情作为上下文
    try:
        post_detail = fetch_post_detail(session, config, msg["link_id"])
    except Exception:
        post_detail = {"title": msg["link_title"], "content": "", "imgs": []}

    # 单独构造回复评论的 prompt，不走通用 template
    system_prompt = prompt[0]  # 人设部分不变

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

        # 优先级3：回复帖子
        for post in pending_posts:
            if not running:
                return
            try:
                reply_to_post(session, config, prompt, post, dry_run)
                replied_count += 1
            except Exception as e:
                log(f"[回复帖子] 出错: {e}")
            log(f"[冷却] 等待 {cooldown} 秒...")
            interruptible_sleep(cooldown)

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
    fetcher = threading.Thread(target=fetch_loop, args=(session, config), daemon=True)
    fetcher.start()

    # 主线程做回复
    try:
        reply_loop(session, config, prompt, dry_run=args.dry_run)
    except KeyboardInterrupt:
        pass

    log("Bot 已停止")


if __name__ == "__main__":
    main()
