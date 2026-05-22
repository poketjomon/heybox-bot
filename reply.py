"""读取未回复帖子，LLM 生成评论并发送"""

import argparse
import os
import time

import yaml

from src.commenter import post_comment
from src.llm import generate_comment, load_prompt
from src.scraper import get_session
from src.storage import read_jsonl, update_record, now_iso

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
POSTS_FILE = os.path.join(DATA_DIR, "posts.jsonl")


def main():
    parser = argparse.ArgumentParser(description="自动回复未评论的帖子")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="试运行，不实际发送评论")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    prompt_path = config.get("prompt_file", "prompt.md")
    prompt = load_prompt(prompt_path)

    posts = read_jsonl(POSTS_FILE)
    pending = [p for p in posts if "comment" not in p]
    print(f"共 {len(posts)} 个帖子，已评论 {len(posts) - len(pending)} 个，待评论 {len(pending)} 个")

    if not pending:
        print("没有待评论的帖子，退出")
        return

    max_comments = config.get("max_comments", 5)
    delay = config.get("delay_seconds", 10)
    session = get_session(config)

    commented = 0
    for post in pending[:max_comments]:
        link_id = post["link_id"]
        print(f"\n--- 帖子 {link_id} ---")
        print(f"标题: {post.get('title', '')[:60]}")

        try:
            print("正在生成评论...")
            comment = generate_comment(config, prompt, post)
            print(f"生成评论: {comment}")

            if args.dry_run:
                print("[DRY RUN] 跳过发送")
            else:
                result = post_comment(session, config, link_id, comment)
                print(f"发送结果: {result}")
                commented += 1

            update_record(POSTS_FILE, link_id, {
                "comment": comment,
                "commented_at": now_iso(),
            })

        except Exception as e:
            print(f"处理帖子 {link_id} 出错: {e}")
            continue

        if commented < max_comments and post != pending[:max_comments][-1]:
            print(f"等待 {delay} 秒...")
            time.sleep(delay)

    print(f"\n完成！共评论 {commented} 个帖子")


if __name__ == "__main__":
    main()
