"""小黑盒 Bot — LLM 生成内容并发帖"""

import argparse
import json
import os

import yaml
from openai import OpenAI

from src.scraper import get_session, _base_params
from src.llm import load_prompt
from src.storage import append_jsonl, now_iso

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
POSTS_SENT_FILE = os.path.join(DATA_DIR, "posts_sent.jsonl")


def create_post(session, config, title, content, topic_ids="7214", hashtags=None, use_html=False):
    """发布帖子到小黑盒

    Args:
        title: 帖子标题
        content: 帖子正文
        topic_ids: 发布到的话题ID，逗号分隔多个
        hashtags: 标签列表，如 ["bot", "日常"]
        use_html: 是否使用 HTML 富文本格式（支持 <a> 链接等）
    """
    url = "https://api.xiaoheihe.cn/bbs/app/api/link/post"
    params = _base_params(config, "/bbs/app/api/link/post")

    content_type = "html" if use_html else "text"
    link_tag = "1" if use_html else "27"
    text_payload = json.dumps([{"text": content, "type": content_type}], ensure_ascii=False)

    data = {
        "text": text_payload,
        "title": title,
        "desc": "",
        "post_type": "1",
        "view_limit": "1",
        "link_tag": link_tag,
        "post_card_ids": "",
        "topic_ids": topic_ids,
        "hashtags": json.dumps(hashtags or [], ensure_ascii=False),
        "original": "1",
        "declaration": "1",
        "extra_declaration": "1",
    }

    resp = session.post(url, params=params, data=data)
    resp.raise_for_status()
    return resp.json()


def generate_post(config, prompt_path, topic=None):
    """用 LLM 生成帖子标题和内容"""
    llm_config = config["llm"]
    client = OpenAI(base_url=llm_config["base_url"], api_key=llm_config["api_key"])

    # 加载人设 prompt（只用 system 部分）
    system_prompt, _ = load_prompt(prompt_path)

    # 加载发帖 prompt
    post_prompt_path = os.path.join(os.path.dirname(prompt_path), "post.md")
    with open(post_prompt_path, "r", encoding="utf-8") as f:
        post_prompt = f.read().strip()

    if topic:
        user_text = f"请你以自己的身份发一个帖子，主题是：{topic}\n\n{post_prompt}"
    else:
        user_text = post_prompt

    response = client.chat.completions.create(
        model=llm_config["model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        max_tokens=800,
        temperature=0.9,
    )

    usage = response.usage
    if usage:
        print(f"  [token] input={usage.prompt_tokens}, output={usage.completion_tokens}, total={usage.total_tokens}")

    result = response.choices[0].message.content.strip()
    result = result.replace('"', " ").replace("\u201c", " ").replace("\u201d", " ")

    # 解析标题、标签和正文
    lines = result.split("\n")
    title = lines[0].strip().lstrip("#").strip()

    # 跳过空行找标签行
    tags = []
    content_start = 1
    for i in range(1, min(len(lines), 5)):
        line = lines[i].strip()
        if not line:
            continue
        # 去掉可能的前缀如 "标签：" "tags:"
        for prefix in ["标签：", "标签:", "tags:", "Tags:"]:
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        # 判断是否是标签行：短、逗号分隔、不像正文开头
        if (line and "," in line or "，" in line) and len(line) < 50 and not line.startswith(("大家", "我", "最近", "今天")):
            tags = [t.strip().strip("#") for t in line.replace("，", ",").split(",") if t.strip()]
            content_start = i + 1
        break

    content = "\n".join(lines[content_start:]).strip()

    return title, content, tags


def main():
    parser = argparse.ArgumentParser(description="小黑盒 Bot 发帖")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--prompt", default=None, help="人设文件路径，默认读 config.yaml 中的 prompt_file")
    parser.add_argument("--topic", default="7214", help="话题ID，默认盒友杂谈")
    parser.add_argument("--subject", default=None, help="指定发帖主题，如 '自我介绍'")
    parser.add_argument("--hashtags", default="", help="标签，逗号分隔，如 'bot,日常'")
    parser.add_argument("--dry-run", action="store_true", help="试运行，只生成不发送")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    prompt_path = args.prompt or config.get("prompt_file", "prompts/warm.md")
    hashtags = [h.strip() for h in args.hashtags.split(",") if h.strip()] if args.hashtags else []

    print(f"[发帖] 人设: {prompt_path}")
    print(f"[发帖] 话题: {args.topic}")
    print()

    # 生成内容
    print("[发帖] 正在生成...")
    title, content, auto_tags = generate_post(config, prompt_path, topic=args.subject)
    # 命令行标签优先，没有则用 LLM 生成的
    if not hashtags and auto_tags:
        hashtags = auto_tags
    print(f"[发帖] 标题: {title}")
    print(f"[发帖] 标签: {hashtags or '无'}")
    print(f"[发帖] 正文 ({len(content)} 字):")
    print(content)
    print()

    if args.dry_run:
        print("[发帖] DRY RUN，未发送")
        append_jsonl(POSTS_SENT_FILE, {
            "title": title,
            "content": content,
            "topic_id": args.topic,
            "hashtags": hashtags,
            "status": "dry_run",
            "posted_at": now_iso(),
        })
        return

    # 发送
    session = get_session(config)
    result = create_post(session, config, title, content, topic_ids=args.topic, hashtags=hashtags)
    if result.get("status") == "ok":
        print(f"[发帖] 发送成功! link_id={result.get('link_id')}")
        append_jsonl(POSTS_SENT_FILE, {
            "link_id": str(result.get("link_id", "")),
            "title": title,
            "content": content,
            "topic_id": args.topic,
            "hashtags": hashtags,
            "status": "success",
            "posted_at": now_iso(),
        })
    else:
        print(f"[发帖] 发送失败: {result.get('msg') or result}")
        append_jsonl(POSTS_SENT_FILE, {
            "title": title,
            "content": content,
            "topic_id": args.topic,
            "hashtags": hashtags,
            "status": "failed",
            "error": result.get("msg") or str(result),
            "posted_at": now_iso(),
        })


if __name__ == "__main__":
    main()
