"""获取@我的消息并自动回复"""

import argparse
import os
import re
import time
from datetime import datetime, timedelta

import yaml

from src.commenter import post_comment_reply
from src.llm import generate_comment, load_prompt
from src.scraper import get_session, _base_params, fetch_post_detail
from src.storage import read_jsonl, append_jsonl, now_iso

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
AT_FILE = os.path.join(DATA_DIR, "at_messages.jsonl")


def fetch_at_messages(session, config, limit=20):
    """获取@我的消息列表"""
    url = "https://api.xiaoheihe.cn/bbs/app/user/message"
    params = _base_params(config, "/bbs/app/user/message")
    params.update({
        "message_type": "16",
        "offset": "0",
        "limit": str(limit),
    })

    resp = session.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()

    messages = []
    for msg in data.get("result", {}).get("messages", []):
        # 清理 comment_a_text 中的 HTML 标签，提取纯文本
        raw_text = msg.get("comment_a_text", "")
        clean_text = re.sub(r"<[^>]+>", "", raw_text).strip()

        messages.append({
            "message_id": str(msg["message_id"]),
            "link_id": str(msg["linkid"]),
            "link_title": msg.get("link_title", ""),
            "comment_a_id": str(msg["comment_a_id"]),
            "root_comment_id": str(msg["root_comment_id"]),
            "comment_text": clean_text,
            "user_a": msg.get("user_a", {}).get("username", ""),
            "userid_a": str(msg.get("userid_a", "")),
            "timestamp": float(msg.get("timestamp", 0)),
        })
    return messages


def main():
    parser = argparse.ArgumentParser(description="回复@我的用户")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="试运行，不实际发送")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    prompt_path = config.get("prompt_file", "prompt.md")
    prompt = load_prompt(prompt_path)

    session = get_session(config)

    # 获取@消息
    print("正在获取@我的消息...")
    messages = fetch_at_messages(session, config)
    print(f"获取到 {len(messages)} 条@消息")

    # 过滤已回复的 + 只回复最近2天的
    replied_ids = {r["message_id"] for r in read_jsonl(AT_FILE) if "message_id" in r}
    cutoff = (datetime.now() - timedelta(days=2)).timestamp()
    pending = [m for m in messages if m["message_id"] not in replied_ids and m["timestamp"] >= cutoff]
    print(f"已回复 {len(messages) - len(pending)} 条，待回复 {len(pending)} 条（仅最近2天）")

    if not pending:
        print("没有待回复的@消息，退出")
        return

    max_comments = config.get("max_comments", 5)
    delay = config.get("delay_seconds", 30)
    replied = 0

    for msg in pending[:max_comments]:
        print(f"\n--- @消息 {msg['message_id']} ---")
        print(f"来自: {msg['user_a']}")
        print(f"帖子: {msg['link_title'][:60]}")
        print(f"内容: {msg['comment_text'][:80]}")

        try:
            # 获取帖子详情
            print("正在获取帖子内容...")
            post_detail = fetch_post_detail(session, config, msg["link_id"])

            # 构造 post_content 给 LLM：帖子内容 + @我的评论
            post_content = {
                "title": post_detail.get("title") or msg["link_title"],
                "content": (
                    f"{post_detail.get('content', '')}\n\n"
                    f"---\n"
                    f"用户「{msg['user_a']}」在评论区@了我，说：{msg['comment_text']}"
                ),
                "imgs": post_detail.get("imgs", []),
            }

            print("正在生成回复...")
            comment = generate_comment(config, prompt, post_content)
            print(f"生成回复: {comment}")

            if args.dry_run:
                print("[DRY RUN] 跳过发送")
                status = "dry_run"
            else:
                result = post_comment_reply(
                    session, config,
                    link_id=msg["link_id"],
                    text=comment,
                    reply_id=msg["comment_a_id"],
                    root_id=msg["root_comment_id"],
                )
                print(f"发送结果: {result}")
                status = "success"
                replied += 1

            # 记录已回复
            append_jsonl(AT_FILE, {
                "message_id": msg["message_id"],
                "link_id": msg["link_id"],
                "user_a": msg["user_a"],
                "comment_text": msg["comment_text"],
                "reply": comment,
                "status": status,
                "replied_at": now_iso(),
            })

        except Exception as e:
            print(f"处理消息 {msg['message_id']} 出错: {e}")
            continue

        if replied < max_comments and msg != pending[:max_comments][-1]:
            print(f"等待 {delay} 秒...")
            time.sleep(delay)

    print(f"\n完成！共回复 {replied} 条@消息")


if __name__ == "__main__":
    main()
