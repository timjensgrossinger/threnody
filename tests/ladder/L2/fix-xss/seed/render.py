"""Renders a comment feed as HTML."""


def render_comment(author, body):
    return (
        '<li class="comment">'
        '<span class="author">' + author + '</span>'
        '<p class="body">' + body + '</p>'
        '</li>'
    )


def render_feed(comments):
    items = "".join(render_comment(a, b) for a, b in comments)
    return '<ul class="feed">' + items + '</ul>'
