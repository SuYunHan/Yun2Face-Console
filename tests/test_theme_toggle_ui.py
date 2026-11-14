from pathlib import Path


def test_theme_toggle_markup_and_script():
    html = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    source = (Path(__file__).parents[1] / "web" / "app.js").read_text(encoding="utf-8")

    # 1. 验证遵照标准在 head 中声明 color-scheme 元标签与防闪烁脚本
    assert '<meta name="color-scheme" content="light dark">' in html
    assert 'localStorage.getItem("color-scheme")' in html

    # 2. 验证导航栏中存在切换按钮及关联事件
    assert 'id="themeToggle"' in html
    assert 'onclick="toggleTheme()"' in html
    assert 'id="themeText"' in html

    # 3. 验证 JavaScript 主题状态切换与持久化逻辑
    assert "function toggleTheme()" in source
    assert "function getResolvedTheme()" in source
    assert "function updateThemeUI()" in source
    assert "function initTheme()" in source
    assert "color-scheme" in source
