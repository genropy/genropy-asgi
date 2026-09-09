"""Documentation serves generated assets, never files outside the snapshot."""
from benchmarks.portal.documentation import DocumentationApplication


def test_documentation_boundary_and_generated_assets(tmp_path):
    root = tmp_path / 'docs'
    root.mkdir()
    (root / 'index.html').write_text('<h1>Documentation</h1>')
    (root / 'search').mkdir()
    (root / 'search/search_index.json').write_text('{"docs":[]}')
    (tmp_path / 'private').write_text('private')
    (root / 'escape').symlink_to(tmp_path / 'private')
    app = DocumentationApplication(directory=root, code='internals')
    assert app.read_document('/')[0] == b'<h1>Documentation</h1>'
    assert app.read_document('/search/search_index.json')[1] == 'application/json'
    assert app.read_document('/../private') is None
    assert app.read_document('/escape') is None
    assert app.read_document('/missing') is None
