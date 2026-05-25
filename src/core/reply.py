"""Bot 回复逻辑"""

import os

from src.commenter import post_comment, post_comment_reply
from src.llm import generate_comment, llm_chat
from src.scraper import fetch_post_detail
from src.storage import read_jsonl, append_jsonl, update_record, now_iso

from src.core.utils import (
    POSTS_FILE, AT_FILE, REPLY_FILE, ROLE_SWITCH_FILE,
    log, trigger_rate_limit,
)


def get_pending_posts():
    """获取待回复的帖子（没有 status=success 的）"""
    posts = read_jsonl(POSTS_FILE)
    return [p for p in posts if p.get("status") not in ("success", "deleted")]


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
            msg_text = result.get("msg") or str(result)
            log(f"[回复帖子] 发送失败: {msg_text}")
            if "频次" in msg_text or "频率" in msg_text:
                trigger_rate_limit(config)
                return "rate_limited"
            elif "屏蔽" in msg_text or "违规" in msg_text:
                log("[回复帖子] 检测到屏蔽词，将重新生成")
                update_record(POSTS_FILE, link_id, {"pending_comment": None})
                return "blocked"
            elif "删除" in msg_text:
                log(f"[回复帖子] 帖子已删除，跳过: {link_id}")
                update_record(POSTS_FILE, link_id, {
                    "status": "deleted",
                    "pending_comment": None,
                })
                return "deleted"
            else:
                update_record(POSTS_FILE, link_id, {"pending_comment": comment})
                return "failed"

    update_record(POSTS_FILE, link_id, {
        "comment": comment,
        "status": "success",
        "commented_at": now_iso(),
        "pending_comment": None,
    })


def reply_to_at(session, config, prompt, msg, dry_run=False):
    """回复一条@消息"""
    log(f"[回复@] 来自 {msg['user_a']} - {msg['comment_text'][:40]}")

    # 检测"转xx"角色切换
    role_switch_dir = config.get("role_switch_dir", "prompts/persona")
    role_whitelist = config.get("role_whitelist", [])
    comment_text = msg["comment_text"]

    import re as _re
    switched_role = None
    if role_whitelist:
        sorted_roles = sorted(role_whitelist, key=len, reverse=True)
        pattern = r"转(" + "|".join(_re.escape(r) for r in sorted_roles) + r")"
        role_match = _re.search(pattern, comment_text)
        if role_match:
            switched_role = role_match.group(1)

    # 记录角色切换
    if switched_role:
        role_records = [r for r in read_jsonl(ROLE_SWITCH_FILE)
                        if r.get("link_id") == msg["link_id"]
                        and r.get("user") == msg["user_a"]]
        last_role = role_records[-1].get("role") if role_records else None
        if switched_role != last_role:
            append_jsonl(ROLE_SWITCH_FILE, {
                "link_id": msg["link_id"],
                "user": msg["user_a"],
                "role": switched_role,
                "switched_at": now_iso(),
            })
            log(f"[角色切换] 用户 {msg['user_a']} 触发: 转{switched_role}")

    # 查找历史角色切换记录
    if not switched_role:
        role_records = [r for r in read_jsonl(ROLE_SWITCH_FILE)
                        if r.get("link_id") == msg["link_id"]
                        and r.get("user") == msg["user_a"]]
        if role_records:
            switched_role = role_records[-1].get("role")

    # 确定 system_prompt
    if switched_role:
        persona_path = os.path.join(role_switch_dir, f"{switched_role}.md")
        if os.path.exists(persona_path):
            with open(persona_path, "r", encoding="utf-8") as f:
                persona_text = f.read().strip()
            log(f"[角色切换] 使用角色文件: {persona_path}")
        else:
            fallback_path = os.path.join(role_switch_dir, "_fallback.md")
            if os.path.exists(fallback_path):
                with open(fallback_path, "r", encoding="utf-8") as f:
                    persona_text = f.read().strip().replace("{{role_name}}", switched_role)
            else:
                persona_text = f"你现在扮演「{switched_role}」这个角色。用符合这个角色身份的语气、口癖和性格来回复。回复要简短自然，不超过30字。"
            log(f"[角色切换] 文件缺失，使用兜底模板: {switched_role}")
        base_path = os.path.join("prompts", "base.md")
        with open(base_path, "r", encoding="utf-8") as f:
            base_text = f.read().strip()
        combined = f"{persona_text}\n\n{base_text}"
        if "========" in combined:
            system_prompt = combined.split("========", 1)[0].strip()
        else:
            system_prompt = combined
    else:
        system_prompt = prompt[0]

    # 获取帖子详情作为上下文
    try:
        post_detail = fetch_post_detail(session, config, msg["link_id"])
    except Exception:
        post_detail = {"title": msg["link_title"], "content": "", "imgs": []}

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

    response = llm_chat(
        client, llm_config["model"],
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
    comment = comment.replace('"', "").replace("\u201c", "").replace("\u201d", "")
    comment = " ".join(comment.split())
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
            msg_text = result.get("msg") or str(result)
            log(f"[回复@] 发送失败: {msg_text}")
            if "频次" in msg_text or "频率" in msg_text:
                trigger_rate_limit(config)
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

    # 检测"转xx"角色切换
    role_switch_dir = config.get("role_switch_dir", "prompts/persona")
    role_whitelist = config.get("role_whitelist", [])
    comment_text = msg["comment_text"]
    switched_role = None

    # 用白名单构造正则，精确匹配"转+角色名"
    import re as _re
    switched_role = None
    if role_whitelist:
        # 按长度降序排列，优先匹配长的（如"假面骑士"优先于"假面"）
        sorted_roles = sorted(role_whitelist, key=len, reverse=True)
        pattern = r"转(" + "|".join(_re.escape(r) for r in sorted_roles) + r")"
        role_match = _re.search(pattern, comment_text)
        if role_match:
            switched_role = role_match.group(1)
        else:
            # 检查是否有"转xx"意图但不在白名单
            general_match = _re.search(r"转([\u4e00-\u9fffa-zA-Z]{1,6})", comment_text)
            if general_match:
                log(f"[角色切换] 不在白名单，忽略: 转{general_match.group(1)}")

    # 如果本次检测到了新的角色切换，记录下来
    if switched_role:
        role_records = [r for r in read_jsonl(ROLE_SWITCH_FILE)
                        if r.get("link_id") == msg["link_id"]
                        and r.get("user") == msg["user_a"]]
        last_role = role_records[-1].get("role") if role_records else None
        if switched_role != last_role:
            append_jsonl(ROLE_SWITCH_FILE, {
                "link_id": msg["link_id"],
                "user": msg["user_a"],
                "role": switched_role,
                "switched_at": now_iso(),
            })
            log(f"[角色切换] 用户 {msg['user_a']} 触发: 转{switched_role}")

    # 查找当前用户是否有角色切换记录（包括刚记录的）
    if not switched_role:
        role_records = [r for r in read_jsonl(ROLE_SWITCH_FILE)
                        if r.get("link_id") == msg["link_id"]
                        and r.get("user") == msg["user_a"]]
        if role_records:
            switched_role = role_records[-1].get("role")

    # 确定使用的 system_prompt
    if switched_role:
        # 从 prompts/persona/xx.md 读取人设
        persona_path = os.path.join(role_switch_dir, f"{switched_role}.md")
        if os.path.exists(persona_path):
            with open(persona_path, "r", encoding="utf-8") as f:
                persona_text = f.read().strip()
            log(f"[角色切换] 使用角色文件: {persona_path}")
        else:
            # 白名单里有但文件不存在，用 _fallback.md 兜底
            fallback_path = os.path.join(role_switch_dir, "_fallback.md")
            if os.path.exists(fallback_path):
                with open(fallback_path, "r", encoding="utf-8") as f:
                    persona_text = f.read().strip().replace("{{role_name}}", switched_role)
            else:
                persona_text = f"你现在扮演「{switched_role}」这个角色。用符合这个角色身份的语气、口癖和性格来回复。回复要简短自然，不超过30字。"
            log(f"[角色切换] 文件缺失，使用兜底模板: {switched_role}")
        # 拼上 base.md（表情包 + 回复模板）
        base_path = os.path.join("prompts", "base.md")
        with open(base_path, "r", encoding="utf-8") as f:
            base_text = f.read().strip()
        combined = f"{persona_text}\n\n{base_text}"
        if "========" in combined:
            system_prompt = combined.split("========", 1)[0].strip()
        else:
            system_prompt = combined
    else:
        system_prompt = prompt[0]

    # 获取帖子详情作为上下文
    try:
        post_detail = fetch_post_detail(session, config, msg["link_id"])
    except Exception:
        post_detail = {"title": msg["link_title"], "content": ""}

    # 聚合同一帖子下与同一用户的对话历史
    max_rounds = config.get("bot", {}).get("reply_history_rounds", 10)
    history_records = [r for r in read_jsonl(REPLY_FILE)
                       if r.get("link_id") == msg["link_id"]
                       and r.get("user_a") == msg["user_a"]
                       and r.get("status") == "success"]
    history_records.sort(key=lambda r: r.get("replied_at", ""))

    # 构造对话历史文本
    history_text = ""
    if history_records:
        history_lines = []
        for r in history_records[-max_rounds:]:
            history_lines.append(f"对方：{r['comment_text']}")
            history_lines.append(f"我：{r['reply']}")
        history_text = "\n".join(history_lines)

    # 构造回复评论的 user prompt
    if history_text:
        user_text = (
            f"帖子标题：{post_detail.get('title') or msg['link_title']}\n\n"
            f"帖子内容：{post_detail.get('content', '')[:300]}\n\n"
            f"---\n"
            f"我和用户「{msg['user_a']}」在这个帖子下的对话历史：\n"
            f"{history_text}\n\n"
            f"现在对方又回复我说：「{msg['comment_text']}」\n\n"
            f"请你继续这个对话，回复对方。要求：\n"
            f"- 针对对方最新说的内容回应\n"
            f"- 不要重复之前说过的话\n"
            f"- 保持你的人设和语气\n"
            f"- 只输出回复内容，不超过30字"
        )
    else:
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

    response = llm_chat(
        client, llm_config["model"],
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
    comment = comment.replace('"', "").replace("\u201c", "").replace("\u201d", "")
    comment = " ".join(comment.split())
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
            msg_text = result.get("msg") or str(result)
            log(f"[回复评论] 发送失败: {msg_text}")
            if "频次" in msg_text or "频率" in msg_text:
                trigger_rate_limit(config)
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
