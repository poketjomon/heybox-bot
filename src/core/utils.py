"""Bot 全局状态和工具函数"""

import os
import time
from datetime import datetime, timedelta


DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")
POSTS_FILE = os.path.join(DATA_DIR, "posts.jsonl")
AT_FILE = os.path.join(DATA_DIR, "at_messages.jsonl")
REPLY_FILE = os.path.join(DATA_DIR, "reply_messages.jsonl")
POSTS_SENT_FILE = os.path.join(DATA_DIR, "posts_sent.jsonl")
HOT_POSTS_FILE = os.path.join(DATA_DIR, "hot_posts.jsonl")
NEWS_SENT_FILE = os.path.join(DATA_DIR, "news_sent.jsonl")
ROLE_SWITCH_FILE = os.path.join(DATA_DIR, "role_switches.jsonl")

running = True
rate_limit_until = None  # 全局冷却截止时间


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def is_in_quiet_hours(config, action="reply"):
    """检查当前是否在静默时间段内，且该功能被暂停
    action: reply / fetch / hot_posts / post / news
    """
    bot_config = config.get("bot", {})
    quiet_hours = bot_config.get("quiet_hours", [])
    if not quiet_hours:
        return False
    quiet_pause = bot_config.get("quiet_pause", ["reply", "fetch", "hot_posts", "post", "news"])
    if action not in quiet_pause:
        return False
    now_hour = datetime.now().hour
    for period in quiet_hours:
        parts = str(period).split("-")
        if len(parts) != 2:
            continue
        start, end = int(parts[0]), int(parts[1])
        if start > end:
            # 跨天，如 23-7 表示 23:00~次日7:00
            if now_hour >= start or now_hour < end:
                return True
        else:
            if start <= now_hour < end:
                return True
    return False


def is_rate_limited():
    """检查是否在全局冷却中"""
    global rate_limit_until
    if rate_limit_until and datetime.now() < rate_limit_until:
        return True
    if rate_limit_until and datetime.now() >= rate_limit_until:
        rate_limit_until = None
        log("[全局冷却] 冷却结束，恢复运行")
    return False


def trigger_rate_limit(config):
    """触发全局冷却"""
    global rate_limit_until
    hours = config.get("bot", {}).get("rate_limit_cooldown", 24)
    rate_limit_until = datetime.now() + timedelta(hours=hours)
    log(f"[全局冷却] 触发频次限制，冷却 {hours} 小时，恢复时间: {rate_limit_until.strftime('%m-%d %H:%M')}")


def interruptible_sleep(seconds):
    """可中断的 sleep，每秒检查 running 状态"""
    for _ in range(int(seconds)):
        if not running:
            return
        time.sleep(1)
