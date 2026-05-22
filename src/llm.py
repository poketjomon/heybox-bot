"""LLM 调用封装"""

from openai import OpenAI


def load_prompt(path="prompt.md"):
    """读取 prompt 文件，按 ======== 分割为 system prompt 和 user prompt 模板"""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()

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

    # 替换占位符，构造 user message content
    user_text = user_template.replace("{{title}}", post_content.get("title", ""))
    user_text = user_text.replace("{{content}}", post_content.get("content", ""))

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

    return response.choices[0].message.content.strip()
