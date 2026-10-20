from __future__ import annotations

from newsbot.article import Article, page_images


def test_page_images_filters_logos_and_small():
    html = """
    <html><body>
      <img src="/logo.png">
      <img src="data:image/png;base64,AAA">
      <img src="https://cdn.example.com/icon.svg">
      <img src="https://cdn.example.com/photo.jpg" width="800" height="600">
      <img src="https://cdn.example.com/thumb.jpg" width="80">
      <img src="https://cdn.example.com/pixel.gif">
    </body></html>
    """
    images = page_images(html, "https://example.com/news/1")
    assert images == ["https://cdn.example.com/photo.jpg"]


def test_page_images_relative_and_srcset():
    html = """
    <img src="/media/a.jpg">
    <img srcset="https://cdn.example.com/b.jpg 1x, https://cdn.example.com/b2.jpg 2x">
    <img data-src="https://cdn.example.com/lazy.jpg">
    """
    images = page_images(html, "https://example.com/news/2")
    assert "https://example.com/media/a.jpg" in images
    assert "https://cdn.example.com/b.jpg" in images
    assert "https://cdn.example.com/lazy.jpg" in images


def test_page_images_limit_and_dedup():
    html = "".join(
        f'<img src="https://cdn.example.com/{i}.jpg" width="700">' for i in range(10)
    )
    assert len(page_images(html, limit=3)) == 3
    html_dup = '<img src="https://cdn.example.com/x.jpg"><img src="https://cdn.example.com/x.jpg">'
    assert page_images(html_dup) == ["https://cdn.example.com/x.jpg"]


def test_page_images_bad_html():
    assert page_images("") == []
    assert page_images("не html") == []


def test_article_defaults_have_images_field():
    article = Article()
    assert article.images == ()
    assert article.image == ""
