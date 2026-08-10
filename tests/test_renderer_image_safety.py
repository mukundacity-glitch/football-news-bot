from pathlib import Path

RENDERER = Path('src/renderer.py').read_text()

def test_renderer_never_uses_story_media_as_player_identity():
    assert 'story.get("media_url")' not in RENDERER

def test_renderer_has_no_unverified_search_provider_fallbacks():
    block = RENDERER[RENDERER.index('def _img_assets(story):'):RENDERER.index('\n\n# X rejects PNG uploads over 5 MB.')]
    assert 'espncdn.com' not in block
    assert 'bbc.co.uk/sport/football/players' not in block
    assert 'fotmob.com/api/search' not in block
    assert 'crest fallback' not in block.lower()

def test_renderer_keeps_verified_wikipedia_fallback():
    block = RENDERER[RENDERER.index('def _img_assets(story):'):RENDERER.index('\n\n# X rejects PNG uploads over 5 MB.')]
    assert '_wikipedia_player_image' in block
    assert 'neutral silhouette' in block.lower()
