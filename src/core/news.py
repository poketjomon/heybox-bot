"""Bot 每日新闻逻辑"""

import os
from datetime import datetime, timedelta

from src.llm import load_prompt, llm_chat
from src.storage import read_jsonl, append_jsonl, now_iso
from src.poster import create_post

from src.core.utils import HOT_POSTS_FILE, NEWS_SENT_FILE, log


def do_daily_news(session, config, prompt_path, dry_run=False):
    """每日新闻：到点后汇总热门帖子并发帖，支持多个时间点"""
    hot_config = config.get("hot_posts", {})
    if not hot_config.get("news_enabled", False):
        return

    # 支持 news_hours 列表或旧的 news_hour 单值
    news_hours = hot_config.get("news_hours", None)
    if not news_hours:
        news_hours = [hot_config.get("news_hour", 10)]
    news_hours = sorted(news_hours)

    now = datetime.now()

    # 找到当前应该触发的时间点：最近一个已过的 news_hour
    current_slot = None
    for h in news_hours:
        if now.hour >= h:
            current_slot = h
    if current_slot is None:
        return  # 还没到今天第一个发布时间

    # 检查这个时间段是否已发过
    news_records = read_jsonl(NEWS_SENT_FILE)
    today_str = now.strftime("%Y-%m-%d")
    for r in news_records:
        if r.get("status") == "success" and r.get("posted_at", "").startswith(today_str):
            try:
                posted = datetime.fromisoformat(r["posted_at"])
                if posted.hour >= current_slot:
                    next_slot = None
                    for h in news_hours:
                        if h > current_slot:
                            next_slot = h
                            break
                    if next_slot is None or posted.hour < next_slot:
                        return
            except (ValueError, KeyError):
                pass

    # 收集已上过新闻的帖子 link_id，避免重复
    used_link_ids = set()
    for r in news_records:
        if r.get("status") == "success":
            for lid in r.get("source_link_ids", []):
                used_link_ids.add(lid)

    # 确定素材时间窗口
    slot_idx = news_hours.index(current_slot)
    if slot_idx == 0:
        prev_hour = news_hours[-1]
        cutoff_start = (now - timedelta(days=1)).replace(hour=prev_hour, minute=0, second=0, microsecond=0)
    else:
        prev_hour = news_hours[slot_idx - 1]
        cutoff_start = now.replace(hour=prev_hour, minute=0, second=0, microsecond=0)
    cutoff_end = now.replace(hour=current_slot, minute=0, second=0, microsecond=0)

    hot_posts = read_jsonl(HOT_POSTS_FILE)
    candidates = []
    seen_titles = set()
    for p in hot_posts:
        try:
            fetched = datetime.fromisoformat(p["fetched_at"])
        except (ValueError, KeyError):
            continue
        if cutoff_start <= fetched <= cutoff_end:
            if p["title"] not in seen_titles and p["link_id"] not in used_link_ids:
                candidates.append(p)
                seen_titles.add(p["title"])

    if not candidates:
        log("[新闻] 没有足够的热门帖子素材，跳过")
        return

    # 按评论数排序，取前 N 条
    max_items = hot_config.get("news_max_items", 15)
    candidates.sort(key=lambda x: x.get("comment_num", 0), reverse=True)
    top_posts = candidates[:max_items]

    # 构造 LLM prompt
    from openai import OpenAI
    llm_config = config["llm"]
    client = OpenAI(base_url=llm_config["base_url"], api_key=llm_config["api_key"])

    system_prompt, _ = load_prompt(prompt_path)

    news_prompt_path = os.path.join(os.path.dirname(prompt_path), "news.md")
    if not os.path.exists(news_prompt_path):
        news_prompt_path = os.path.join(os.path.dirname(os.path.dirname(prompt_path)), "news.md")
    with open(news_prompt_path, "r", encoding="utf-8") as f:
        news_prompt = f.read().strip()

    # 拼接帖子列表（含热评）
    posts_parts = []
    for i, p in enumerate(top_posts):
        line = f"{i+1}. 【{p['title']}】({p.get('comment_num', 0)}条评论) — {(p.get('content') or p.get('description', ''))[:150]}"
        comments = p.get("top_comments", [])
        if comments:
            hot_comments = " | ".join(f"{c['text'][:40]}({c['up']}赞)" for c in comments)
            line += f"\n   热评: {hot_comments}"
        posts_parts.append(line)
    posts_text = "\n".join(posts_parts)
    user_text = f"{news_prompt}\n\n{posts_text}"

    log(f"[新闻] 正在生成每日快讯（{len(top_posts)} 条素材）...")
    try:
        response = llm_chat(
            client, llm_config["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            max_tokens=1500,
            temperature=0.8,
        )
    except Exception as e:
        log(f"[新闻] LLM 生成失败: {e}")
        return

    usage = response.usage
    if usage:
        log(f"  [token] input={usage.prompt_tokens}, output={usage.completion_tokens}, total={usage.total_tokens}")

    result = response.choices[0].message.content.strip()
    result = result.replace('"', "").replace("\u201c", "").replace("\u201d", "")

    # 正文就是 LLM 的完整输出
    content = result.strip()

    # 在每条帖子点评中，将短标题变成可点击链接
    import re as _re
    lines = content.split("\n")
    new_lines = []
    for line in lines:
        m = _re.match(r"^(\d+)\.\s*(.+?)\s*[—–-]\s*(.+)$", line)
        if m:
            idx = int(m.group(1)) - 1
            short_title = m.group(2)
            comment = m.group(3)
            if 0 <= idx < len(top_posts):
                p = top_posts[idx]
                link = f'<a href="https://www.xiaoheihe.cn/app/bbs/link/{p["link_id"]}" target="_blank">{short_title}</a>'
                new_lines.append(f"{m.group(1)}. {link} — {comment}")
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)
    content = "\n".join(new_lines)

    # 生成标题
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday = weekdays[now.weekday()]
    success_count = sum(1 for r in news_records if r.get("status") == "success")
    issue_num = success_count + 1
    title = f"{now.strftime('%m月%d日')} {weekday} 热点追踪（第{issue_num}期）"

    # 转换为 HTML 格式
    html_lines = []
    for line in content.split("\n"):
        if line.strip():
            html_lines.append(f"<p>{line}</p>")
        else:
            html_lines.append("<p><br/></p>")
    content = "".join(html_lines)

    log(f"[新闻] 标题: {title}")
    log(f"[新闻] 正文 ({len(content)} 字)")

    if dry_run:
        log("[新闻] DRY RUN，未发送")
        append_jsonl(NEWS_SENT_FILE, {
            "title": title,
            "content": content,
            "post_count": len(top_posts),
            "status": "dry_run",
            "posted_at": now_iso(),
        })
        return

    topic_id = hot_config.get("news_topic_id", "7214")
    try:
        result_resp = create_post(session, config, title, content, topic_ids=topic_id, hashtags=["每日热点"], use_html=True)
        if result_resp.get("status") == "ok":
            log(f"[新闻] 发送成功! link_id={result_resp.get('link_id')}")
            append_jsonl(NEWS_SENT_FILE, {
                "link_id": str(result_resp.get("link_id", "")),
                "title": title,
                "content": content,
                "post_count": len(top_posts),
                "source_link_ids": [p["link_id"] for p in top_posts],
                "status": "success",
                "posted_at": now_iso(),
            })
        else:
            log(f"[新闻] 发送失败: {result_resp.get('msg') or result_resp}")
            append_jsonl(NEWS_SENT_FILE, {
                "title": title,
                "content": content,
                "post_count": len(top_posts),
                "status": "failed",
                "error": result_resp.get("msg") or str(result_resp),
                "posted_at": now_iso(),
            })
    except Exception as e:
        log(f"[新闻] 发送出错: {e}")
