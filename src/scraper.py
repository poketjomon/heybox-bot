"""小黑盒帖子抓取（通过 API）"""

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
    url = "https://api.xiaoheihe.cn/bbs/app/link/info"
    params = _base_params(config, "/bbs/app/link/info")
    params["link_id"] = str(link_id)

    resp = session.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()

    link_info = data.get("result", {}).get("link_info", {})
    return {
        "title": link_info.get("title", ""),
        "content": link_info.get("description", ""),
        "imgs": link_info.get("imgs", []),
    }


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
