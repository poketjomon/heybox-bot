"""小黑盒发帖功能"""

import json
import os

from openai import OpenAI

from src.scraper import _base_params
from src.llm import load_prompt, llm_chat
from src.storage import append_jsonl, now_iso

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
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

    # 加载发帖 prompt（先找同目录，找不到就往上一级找）
    post_prompt_path = os.path.join(os.path.dirname(prompt_path), "post.md")
    if not os.path.exists(post_prompt_path):
        post_prompt_path = os.path.join(os.path.dirname(os.path.dirname(prompt_path)), "post.md")
    with open(post_prompt_path, "r", encoding="utf-8") as f:
        post_prompt = f.read().strip()

    if topic:
        user_text = f"请你以自己的身份发一个帖子，主题是：{topic}\n\n{post_prompt}"
    else:
        user_text = post_prompt

    response = llm_chat(
        client, llm_config["model"],
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
        for prefix in ["标签：", "标签:", "tags:", "Tags:"]:
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        if (line and "," in line or "，" in line) and len(line) < 50 and not line.startswith(("大家", "我", "最近", "今天")):
            tags = [t.strip().strip("#") for t in line.replace("，", ",").split(",") if t.strip()]
            content_start = i + 1
        break

    content = "\n".join(lines[content_start:]).strip()

    return title, content, tags
