"""小黑盒帖子抓取（通过 API）"""

import json
import re

import requests

from src.sign import generate_sign


def get_session(config):
    """创建带 cookie 的 requests session"""
    session = requests.Session()

    cookie = config["cookie"]
    if isinstance(cookie, dict):
        for k, v in cookie.items():
            session.cookies.set(k, v)
    else:
        # 字符串格式，解析为 cookie
        for pair in cookie.split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                session.cookies.set(k.strip(), v.strip())

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "Origin": "https://www.xiaoheihe.cn",
        "Referer": "https://www.xiaoheihe.cn/",
    })
    return session


def _generate_nonce():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=32))


def _base_params(config, url_path):
    """构造通用请求参数（自动生成签名）"""
    sign = generate_sign(url_path)
    return {
        "os_type": "web",
        "app": "heybox",
        "client_type": "web",
        "version": "999.0.4",
        "web_version": "2.5",
        "x_client_type": "web",
        "x_app": "heybox_website",
        "heybox_id": config["heybox_id"],
        "x_os_type": "Mac",
        "device_info": "Chrome",
        "device_id": config["device_id"],
        "hkey": sign["hkey"],
        "_time": sign["_time"],
        "nonce": sign["nonce"],
    }


def fetch_post_detail(session, config, link_id):
    """获取单个帖子详情，返回 {title, content, imgs}"""
    url = "https://api.xiaoheihe.cn/bbs/app/link/tree"
    params = _base_params(config, "/bbs/app/link/tree")
    params["link_id"] = str(link_id)
    params["is_first"] = "1"
    params["page"] = "1"
    params["limit"] = "1"

    resp = session.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()

    link = data.get("result", {}).get("link", {})

    # text 字段是 JSON 数组格式的富文本，需要解析提取纯文本
    text_raw = link.get("text", "")
    content = ""
    if text_raw:
        try:
            text_list = json.loads(text_raw)
            parts = []
            for block in text_list:
                t = block.get("text", "")
                clean = re.sub(r"<[^>]+>", "", t).strip()
                if clean:
                    parts.append(clean)
            content = "\n".join(parts)
        except (json.JSONDecodeError, TypeError):
            content = ""

    if not content:
        content = link.get("description", "")

    return {
        "title": link.get("title", ""),
        "content": content,
    }


def fetch_top_comments(session, config, link_id, limit=6):
    """获取帖子热门评论（按点赞排序），返回 [{text, up, username}, ...]"""
    url = "https://api.xiaoheihe.cn/bbs/app/link/tree"
    params = _base_params(config, "/bbs/app/link/tree")
    params["link_id"] = str(link_id)
    params["is_first"] = "1"
    params["page"] = "1"
    params["limit"] = str(limit)

    resp = session.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for floor in data.get("result", {}).get("comments", []):
        for c in floor.get("comment", []):
            text = re.sub(r"<[^>]+>", "", c.get("text", "")).strip()
            if text:
                results.append({
                    "text": text,
                    "up": c.get("up", 0),
                    "username": c.get("user", {}).get("username", ""),
                })
            break  # 只取每层楼主评论

    # 按点赞排序取前 limit 条
    results.sort(key=lambda x: x["up"], reverse=True)
    return results[:limit]


def fetch_post_links(session, config, topic_id="7214", limit=10, sort_filter="new"):
    """从话题 API 获取帖子列表

    返回 [{link_id, title, description, imgs, comment_num}, ...]
    """
    url = "https://api.xiaoheihe.cn/bbs/app/topic/feeds"
    params = _base_params(config, "/bbs/app/topic/feeds")
    params.update({
        "topic_id": topic_id,
        "offset": "0",
        "limit": str(limit),
        "lastval": "",
        "dw": "506",
        "sort_filter": sort_filter,
    })

    resp = session.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()

    posts = []
    for link in data.get("result", {}).get("links", []):
        posts.append({
            "link_id": str(link["linkid"]),
            "title": link.get("title", ""),
            "description": link.get("description", ""),
            "imgs": link.get("imgs", []),
            "comment_num": link.get("comment_num", 0),
        })
    return posts


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
        if "linkid" not in msg or "comment_a_id" not in msg:
            continue

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
