"""LLM 调用封装"""

import os

from openai import OpenAI


def load_prompt(path="prompts/warm.md"):
    """读取 prompt 文件，自动合并 base.md

    人设文件（如 warm.md）只包含人设部分，
    base.md 包含通用回复要求 + ======== + user template。
    合并后按 ======== 分割为 system prompt 和 user prompt 模板。
    """
    with open(path, "r", encoding="utf-8") as f:
        persona = f.read().strip()

    # 加载 base.md（先找同目录，找不到就往上一级找）
    base_path = os.path.join(os.path.dirname(path), "base.md")
    if not os.path.exists(base_path):
        base_path = os.path.join(os.path.dirname(os.path.dirname(path)), "base.md")
    with open(base_path, "r", encoding="utf-8") as f:
        base = f.read().strip()

    # 合并：人设 + 空行 + base
    content = f"{persona}\n\n{base}"

    if "========" in content:
        parts = content.split("========", 1)
        return parts[0].strip(), parts[1].strip()
    else:
        return content, "标题：{{title}}\n\n内容：{{content}}"


def generate_comment(config, prompt, post_content):
    """调用 LLM 生成评论，支持图片"""
    llm_config = config["llm"]
    client = OpenAI(
        base_url=llm_config["base_url"],
        api_key=llm_config["api_key"],
    )

    system_prompt, user_template = prompt

    # 内容过长时要求先省流再评论
    content = post_content.get("content", "")
    max_content_len = config.get("max_content_len", 500)
    if len(content) > max_content_len:
        summary_hint = "\n\n（这篇内容较长，请先用一句话省流总结，然后换行写你的评论。格式：省流：xxx\\n你的评论）"
    else:
        summary_hint = ""

    # 替换占位符，构造 user message content
    user_text = user_template.replace("{{title}}", post_content.get("title", ""))
    user_text = user_text.replace("{{content}}", content)
    user_text += summary_hint

    # 处理图片
    imgs = post_content.get("imgs", [])
    vision_imgs = config.get("vision_imgs", 1)
    img_blocks = []
    for img_url in imgs[:vision_imgs]:
        img_blocks.append({
            "type": "image_url",
            "image_url": {"url": img_url},
        })

    # 按 {{imgs}} 占位符位置插入图片
    user_content = []
    if "{{imgs}}" in user_text:
        parts = user_text.split("{{imgs}}", 1)
        if parts[0].strip():
            user_content.append({"type": "text", "text": parts[0].strip()})
        user_content.extend(img_blocks)
        if parts[1].strip():
            user_content.append({"type": "text", "text": parts[1].strip()})
    else:
        user_content.append({"type": "text", "text": user_text})
        user_content.extend(img_blocks)

    response = client.chat.completions.create(
        model=llm_config["model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        max_tokens=200,
        temperature=0.8,
    )

    usage = response.usage
    if usage:
        print(f"  [token] input={usage.prompt_tokens}, output={usage.completion_tokens}, total={usage.total_tokens}")

    result = response.choices[0].message.content.strip()
    # 去掉所有双引号（中英文），替换为空格
    result = result.replace('"', " ").replace("\u201c", " ").replace("\u201d", " ")
    # 清理多余空格
    result = " ".join(result.split())
    return result
