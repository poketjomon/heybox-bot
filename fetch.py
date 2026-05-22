"""抓取小黑盒帖子，存入 data/posts.jsonl"""

import argparse
import os

import yaml

from src.scraper import fetch_post_links, get_session
from src.storage import append_jsonl, get_existing_ids, now_iso

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
POSTS_FILE = os.path.join(DATA_DIR, "posts.jsonl")


def main():
    parser = argparse.ArgumentParser(description="抓取小黑盒帖子")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    session = get_session(config)
    topic_ids = config.get("topic_ids", None)
    if not topic_ids:
        topic_ids = [config.get("topic_id", "7214")]
    limit = config.get("fetch_limit", 10)
    sort_filter = config.get("sort_filter", "new")

    existing_ids = get_existing_ids(POSTS_FILE)
    max_comment_num = config.get("max_comment_num", 20)
    max_imgs = config.get("max_imgs", 3)
    total_new = 0

    for topic_id in topic_ids:
        print(f"正在获取话题 {topic_id} 的帖子列表（排序: {sort_filter}）...")
        posts = fetch_post_links(session, config, topic_id=topic_id, limit=limit, sort_filter=sort_filter)
        print(f"API 返回 {len(posts)} 个帖子")

        new_posts = [p for p in posts if p["link_id"] not in existing_ids]

        # 按评论数和图片数筛选
        new_posts = [p for p in new_posts
                     if p.get("comment_num", 0) < max_comment_num
                     and len(p.get("imgs", [])) <= max_imgs]

        for post in new_posts:
            record = {
                "link_id": post["link_id"],
                "title": post["title"],
                "content": post["description"],
                "imgs": post.get("imgs", []),
                "comment_num": post.get("comment_num", 0),
                "topic_id": topic_id,
                "fetched_at": now_iso(),
            }
            append_jsonl(POSTS_FILE, record)
            existing_ids.add(post["link_id"])
            print(f"  + [{post['link_id']}] {post['title'][:50]}")

        total_new += len(new_posts)

    print(f"\n抓取完成，新增 {total_new} 条")

    print(f"抓取完成，新增 {len(new_posts)} 条")


if __name__ == "__main__":
    main()
