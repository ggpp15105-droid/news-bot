from __future__ import annotations

from newsbot.article import Article, jsonld_images, link_image, page_images


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


def test_jsonld_images_dict_list_and_url():
    html = """
    <script type="application/ld+json">
    {"@type":"NewsArticle","image":{"@type":"ImageObject","url":"https://cdn.example.com/a.jpg"}}
    </script>
    <script type="application/ld+json">
    {"@graph":[{"image":["https://cdn.example.com/b.jpg","https://cdn.example.com/c.jpg"]}]}
    </script>
    """
    images = jsonld_images(html, "https://example.com/news/3")
    assert "https://cdn.example.com/a.jpg" in images
    assert "https://cdn.example.com/b.jpg" in images


def test_jsonld_images_broken_and_relative():
    html = '<script type="application/ld+json">{битый json</script>'
    assert jsonld_images(html) == []
    html_rel = '<script type="application/ld+json">{"image":"/media/x.jpg"}</script>'
    assert jsonld_images(html_rel, "https://example.com/news/4") == [
        "https://example.com/media/x.jpg"
    ]


def test_link_image_rel():
    html = '<link rel="image_src" href="https://cdn.example.com/preview.png">'
    assert link_image(html) == "https://cdn.example.com/preview.png"
    assert link_image('<link rel="image_src" href="/i.png">', "https://site.ru/a") == "https://site.ru/i.png"
    assert link_image("") == ""


def test_page_images_skips_counters_and_banners():
    html = """
    <img src="https://top-fwz1.mail.ru/counter?id=1">
    <img src="https://cdn.example.com/banner.jpg" width="900">
    <img src="https://cdn.example.com/story.jpg" width="900">
    """
    assert page_images(html) == ["https://cdn.example.com/story.jpg"]
