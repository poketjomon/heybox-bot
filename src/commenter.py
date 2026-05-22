"""小黑盒评论发送"""

from src.scraper import _base_params


def post_comment(session, config, link_id, text):
    """发送评论到小黑盒 API"""
    url = "https://api.xiaoheihe.cn/bbs/app/comment/create"

    params = _base_params(config)
    data = {
        "link_id": link_id,
        "text": text,
        "is_cy": "0",
        "reply_id": "-1",
        "root_id": "-1",
    }

    resp = session.post(url, params=params, data=data)
    resp.raise_for_status()
    return resp.json()
