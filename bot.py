"""小黑盒 Bot — 自动抓取 + 自动回复，持续运行"""

import os
import signal
import threading
import time
from datetime import datetime, timedelta

import yaml

from src.llm import load_prompt
from src.scraper import get_session, fetch_at_messages
from src.storage import read_jsonl

from src.core import utils as bot_utils
from src.core.utils import (
    AT_FILE, REPLY_FILE,
    log, is_in_quiet_hours, is_rate_limited, interruptible_sleep,
)
from src.core.fetch import do_fetch_posts, do_fetch_at, fetch_reply_messages, do_fetch_replies, do_fetch_hot_posts
from src.core.post import do_auto_post
from src.core.news import do_daily_news
from src.core.reply import get_pending_posts, reply_to_post, reply_to_at, reply_to_reply


# ─── 抓取循环 ───────────────────────────────────────────────

def fetch_loop(session, config, prompt_path, dry_run=False):
    """定时抓取线程：按优先级依次抓取 @消息 → 回复评论 → 新帖子 → 自动发帖"""
    bot_config = config.get("bot", {})
    fetch_interval = bot_config.get("fetch_interval", 300)
    fetch_cooldown = bot_config.get("fetch_cooldown", 10)

    while bot_utils.running:
        # 全局冷却时全部暂停
        if is_rate_limited():
            interruptible_sleep(60)
            continue

        # 优先级1：抓取@消息（属于 fetch）
        if not is_in_quiet_hours(config, "fetch"):
            do_fetch_at(session, config)
            interruptible_sleep(fetch_cooldown)
            if not bot_utils.running:
                break

            # 优先级2：抓取回复评论
            do_fetch_replies(session, config)
            interruptible_sleep(fetch_cooldown)
            if not bot_utils.running:
                break

            # 优先级3：抓取新帖子
            do_fetch_posts(session, config)
            interruptible_sleep(fetch_cooldown)
            if not bot_utils.running:
                break

        # 自动发帖（检查是否到时间）
        if not is_in_quiet_hours(config, "post"):
            do_auto_post(session, config, prompt_path, dry_run)
            interruptible_sleep(fetch_cooldown)
            if not bot_utils.running:
                break

        # 抓取热门帖子
        if not is_in_quiet_hours(config, "hot_posts"):
            do_fetch_hot_posts(session, config)
            interruptible_sleep(fetch_cooldown)
            if not bot_utils.running:
                break

        # 每日新闻（检查是否到发布时间）
        if not is_in_quiet_hours(config, "news"):
            do_daily_news(session, config, prompt_path, dry_run)

        # 等待下一轮
        log(f"[抓取] 下一轮抓取在 {fetch_interval} 秒后")
        interruptible_sleep(fetch_interval)


# ─── 回复循环 ───────────────────────────────────────────────

def reply_loop(session, config, prompt, dry_run=False):
    """主线程：逐条回复，按冷却时间间隔。优先级：@消息 > 回复评论 > 帖子"""
    bot_config = config.get("bot", {})
    cooldown = bot_config.get("reply_cooldown", 180)

    while bot_utils.running:
        # 检查全局冷却
        if is_rate_limited():
            remaining = (bot_utils.rate_limit_until - datetime.now()).total_seconds()
            log(f"[全局冷却] 冷却中，剩余 {int(remaining // 3600)}h{int((remaining % 3600) // 60)}m")
            interruptible_sleep(60)
            continue

        # 检查静默时间段
        if is_in_quiet_hours(config, "reply"):
            log("[静默] 当前处于静默时间段，暂停操作")
            interruptible_sleep(60)
            continue

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
                if not bot_utils.running:
                    return
                time.sleep(1)
            continue

        log(f"[回复] 待回复: {len(pending_at)} @消息, {len(pending_replies)} 回复评论, {len(pending_posts)} 帖子")

        replied_count = 0

        # 优先级1：回复@消息
        for msg in pending_at:
            if not bot_utils.running:
                return
            if is_rate_limited():
                break
            try:
                reply_to_at(session, config, prompt, msg, dry_run)
                replied_count += 1
            except Exception as e:
                log(f"[回复@] 出错: {e}")
            log(f"[冷却] 等待 {cooldown} 秒...")
            interruptible_sleep(cooldown)

        # 优先级2：回复"回复我的评论"
        for msg in pending_replies:
            if not bot_utils.running:
                return
            if is_rate_limited():
                break
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
            if not bot_utils.running:
                return
            if is_rate_limited():
                break
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
                        if not bot_utils.running:
                            return
                        try:
                            reply_to_at(session, config, prompt, msg, dry_run)
                            replied_count += 1
                        except Exception as e:
                            log(f"[回复@] 出错: {e}")
                        interruptible_sleep(cooldown)
                    for msg in urgent_replies:
                        if not bot_utils.running:
                            return
                        try:
                            reply_to_reply(session, config, prompt, msg, dry_run)
                            replied_count += 1
                        except Exception as e:
                            log(f"[回复评论] 出错: {e}")
                        interruptible_sleep(cooldown)

            try:
                result = reply_to_post(session, config, prompt, post, dry_run)
                if result == "blocked":
                    log(f"[冷却] 屏蔽词重试，等待 10 秒...")
                    interruptible_sleep(10)
                elif result == "rate_limited":
                    break
                else:
                    replied_count += 1
                    log(f"[冷却] 等待 {cooldown} 秒...")
                    interruptible_sleep(cooldown)
            except Exception as e:
                log(f"[回复帖子] 出错: {e}")
                log(f"[冷却] 等待 {cooldown} 秒...")
                interruptible_sleep(cooldown)
            post_count += 1

        log(f"[回复] 本轮完成，共回复 {replied_count} 条")


# ─── 入口 ────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="小黑盒 Bot 自动运行")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="试运行，不实际发送")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 如果 cookie/cookie.json 存在，从中读取 cookie 覆盖 config
    import json
    cookie_file = os.path.join(os.path.dirname(__file__), "cookie", "cookie.json")
    if os.path.exists(cookie_file):
        with open(cookie_file, "r", encoding="utf-8") as f:
            cookie_list = json.load(f)
        cookie_dict = {item["name"]: item["value"] for item in cookie_list}
        config["cookie"] = cookie_dict
        # 同步 heybox_id
        if "heybox_id" in cookie_dict:
            config["heybox_id"] = cookie_dict["heybox_id"]
        log(f"[配置] 从 cookie/cookie.json 加载 cookie ({len(cookie_dict)} 项)")

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
    quiet_hours = bot_config.get("quiet_hours", [])
    if quiet_hours:
        log(f"  静默时段: {', '.join(str(h) for h in quiet_hours)}")
    log(f"  频次冷却: {bot_config.get('rate_limit_cooldown', 24)}h")
    if args.dry_run:
        log("  模式: DRY RUN（不实际发送）")
    log("=" * 50)

    # 优雅退出
    def handle_signal(sig, frame):
        log("\n收到退出信号，正在停止...")
        bot_utils.running = False

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
