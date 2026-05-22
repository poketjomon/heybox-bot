"""小黑盒评论发送"""

from src.sign import generate_sign
from src.scraper import _base_params


def post_comment(session, config, link_id, text):
    """发送评论到小黑盒 API（顶层评论）"""
    return post_comment_reply(session, config, link_id, text, reply_id="-1", root_id="-1")


def post_comment_reply(session, config, link_id, text, reply_id="-1", root_id="-1"):
    """发送评论/回复到小黑盒 API

    reply_id: 回复的目标评论 ID（-1 表示顶层评论）
    root_id: 根评论 ID（-1 表示顶层评论）
    """
    url = "https://api.xiaoheihe.cn/bbs/app/comment/create"

    params = _base_params(config, "/bbs/app/comment/create")
    data = {
        "link_id": link_id,
        "text": text,
        "is_cy": "0",
        "reply_id": str(reply_id),
        "root_id": str(root_id),
    }

    resp = session.post(url, params=params, data=data)
    resp.raise_for_status()
    return resp.json()
